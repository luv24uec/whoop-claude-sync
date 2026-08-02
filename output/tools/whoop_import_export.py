#!/usr/bin/env python3
"""
whoop_import_export.py — load a WHOOP data-export archive into the database.

Why this exists: the WHOOP developer API does not expose **journal entries**
(alcohol, caffeine, late meals, stress, supplements — the behaviour data). Those
only come out through the in-app data export. This importer folds them into the
same database the API sync writes to, so the insights engine can correlate
behaviours against recovery.

It also backfills history that predates your developer app, and it is completely
tolerant of WHOOP renaming columns — it matches headers by keyword.

How to get the export:
  WHOOP app → Menu → Settings → Data Export → request. You get an email with a
  zip within a few minutes.

  python3 whoop_import_export.py ~/Downloads/whoop_export.zip
  python3 whoop_import_export.py ~/Downloads/my_whoop_data/     # unzipped folder
"""

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime

DATA_DIR = os.path.expanduser(os.environ.get("WHOOP_DATA_DIR", "~/WhoopData"))
DB_PATH = os.path.join(DATA_DIR, "whoop.db")


def norm(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def find_col(header, *keywords, exclude=()):
    """Find the column whose normalised name contains all keywords."""
    for i, name in enumerate(header):
        n = norm(name)
        if all(k in n for k in keywords) and not any(x in n for x in exclude):
            return i
    return None


def parse_day(value):
    """WHOOP export timestamps look like '2026-07-30 23:11:04' or ISO-8601."""
    if not value:
        return None
    text = value.strip().replace("Z", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y-%m-%d %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:len(datetime.now().strftime(fmt))], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19]).date().isoformat()
    except ValueError:
        return None


def to_float(value):
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text or text.lower() in ("na", "n/a", "null", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def iter_csvs(path):
    """Yield (filename, list_of_rows) for every CSV in a zip, folder, or file."""
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for name in files:
                if name.lower().endswith(".csv"):
                    with open(os.path.join(root, name), newline="",
                              encoding="utf-8-sig", errors="replace") as fh:
                        yield name, list(csv.reader(fh))
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.filename.lower().endswith(".csv") and not info.is_dir():
                    with zf.open(info) as raw:
                        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")
                        yield os.path.basename(info.filename), list(csv.reader(text))
    elif path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            yield os.path.basename(path), list(csv.reader(fh))
    else:
        raise SystemExit(f"{path} is not a zip, folder, or CSV.")


# --------------------------------------------------------------------------
# Importers
# --------------------------------------------------------------------------

def import_journal(conn, rows):
    header = rows[0]
    c_start = find_col(header, "cycle", "start") or find_col(header, "start")
    c_q = find_col(header, "question")
    c_a = find_col(header, "answer")
    if c_q is None or c_a is None:
        return 0
    n = 0
    for row in rows[1:]:
        if len(row) <= max(c_q, c_a):
            continue
        day = parse_day(row[c_start]) if c_start is not None else None
        if not day:
            continue
        conn.execute(
            "INSERT INTO journal (day, question, answer) VALUES (?,?,?) "
            "ON CONFLICT(day, question) DO UPDATE SET answer=excluded.answer",
            (day, row[c_q].strip(), row[c_a].strip()),
        )
        n += 1
    return n


def import_cycles(conn, rows):
    """physiological_cycles.csv — recovery, strain, RHR, HRV per day."""
    header = rows[0]
    cols = {
        "start": find_col(header, "cycle", "start") or find_col(header, "start"),
        "strain": find_col(header, "strain", exclude=("scaled",)),
        "kj": find_col(header, "energy", "burned") or find_col(header, "calor"),
        "recovery": find_col(header, "recovery", "score"),
        "rhr": find_col(header, "resting", "heart"),
        "hrv": find_col(header, "heart", "rate", "variability") or find_col(header, "hrv"),
        "avg_hr": find_col(header, "average", "hr") or find_col(header, "average", "heart"),
        "max_hr": find_col(header, "max", "hr") or find_col(header, "max", "heart"),
    }
    if cols["start"] is None:
        return 0
    n = 0
    for row in rows[1:]:
        def val(key):
            i = cols[key]
            return to_float(row[i]) if i is not None and i < len(row) else None

        day = parse_day(row[cols["start"]])
        if not day:
            continue

        kcal = val("kj")
        # The export reports calories (kcal); the API reports kilojoules.
        kj = kcal * 4.184 if kcal else None

        existing = conn.execute("SELECT id FROM cycles WHERE day=?", (day,)).fetchone()
        if not existing:
            # Synthetic negative id keeps export-only rows from colliding with API ids.
            synthetic = -int(day.replace("-", ""))
            conn.execute(
                "INSERT OR IGNORE INTO cycles (id, day, start_ts, score_state, strain, "
                "kilojoule, average_heart_rate, max_heart_rate, raw) "
                "VALUES (?,?,?,'SCORED',?,?,?,?,'{\"source\":\"export\"}')",
                (synthetic, day, row[cols["start"]], val("strain"), kj,
                 val("avg_hr"), val("max_hr")),
            )
            cycle_id = synthetic
        else:
            cycle_id = existing[0]

        if val("recovery") is not None:
            conn.execute(
                "INSERT INTO recovery (cycle_id, day, score_state, recovery_score, "
                "resting_heart_rate, hrv_rmssd_milli, raw) "
                "VALUES (?,?,'SCORED',?,?,?,'{\"source\":\"export\"}') "
                "ON CONFLICT(cycle_id) DO UPDATE SET "
                "recovery_score=COALESCE(recovery.recovery_score, excluded.recovery_score), "
                "resting_heart_rate=COALESCE(recovery.resting_heart_rate, excluded.resting_heart_rate), "
                "hrv_rmssd_milli=COALESCE(recovery.hrv_rmssd_milli, excluded.hrv_rmssd_milli)",
                (cycle_id, day, val("recovery"), val("rhr"), val("hrv")),
            )
        n += 1
    return n


def import_sleeps(conn, rows):
    header = rows[0]
    c_start = find_col(header, "sleep", "onset") or find_col(header, "cycle", "start") \
        or find_col(header, "start")
    c_end = find_col(header, "wake", "onset") or find_col(header, "cycle", "end") \
        or find_col(header, "end")
    if c_end is None:
        return 0
    cols = {
        "perf": find_col(header, "sleep", "performance"),
        "eff": find_col(header, "sleep", "efficiency"),
        "cons": find_col(header, "sleep", "consistency"),
        "rem": find_col(header, "rem", "duration"),
        "sws": find_col(header, "deep", "duration") or find_col(header, "sws", "duration"),
        "light": find_col(header, "light", "duration"),
        "awake": find_col(header, "awake", "duration"),
        "inbed": find_col(header, "in", "bed", "duration"),
        "resp": find_col(header, "respiratory"),
        "dist": find_col(header, "disturbance"),
        "nap": find_col(header, "nap"),
    }
    n = 0
    for row in rows[1:]:
        def val(key):
            i = cols[key]
            return to_float(row[i]) if i is not None and i < len(row) else None

        day = parse_day(row[c_end])
        if not day:
            continue
        sleep_id = f"export-{day}-{'nap' if (row[cols['nap']].strip().lower() in ('true', 'yes') if cols['nap'] is not None and cols['nap'] < len(row) else False) else 'main'}"
        is_nap = 1 if sleep_id.endswith("nap") else 0
        minutes = lambda v: v * 60000 if v is not None else None  # export durations are minutes

        conn.execute(
            "INSERT INTO sleep (id, day, start_ts, end_ts, nap, score_state, "
            "total_in_bed_milli, total_awake_milli, total_light_milli, total_sws_milli, "
            "total_rem_milli, disturbance_count, respiratory_rate, sleep_performance_pct, "
            "sleep_consistency_pct, sleep_efficiency_pct, raw) "
            "VALUES (?,?,?,?,?,'SCORED',?,?,?,?,?,?,?,?,?,?,'{\"source\":\"export\"}') "
            "ON CONFLICT(id) DO NOTHING",
            (sleep_id, day,
             row[c_start] if c_start is not None and c_start < len(row) else None,
             row[c_end], is_nap,
             minutes(val("inbed")), minutes(val("awake")), minutes(val("light")),
             minutes(val("sws")), minutes(val("rem")),
             val("dist"), val("resp"), val("perf"), val("cons"), val("eff")),
        )
        n += 1
    return n


def import_workouts(conn, rows):
    header = rows[0]
    c_start = find_col(header, "workout", "start") or find_col(header, "start")
    c_end = find_col(header, "workout", "end") or find_col(header, "end")
    c_sport = find_col(header, "activity", "name") or find_col(header, "sport")
    if c_start is None:
        return 0
    cols = {
        "strain": find_col(header, "activity", "strain") or find_col(header, "strain"),
        "kcal": find_col(header, "energy", "burned") or find_col(header, "calor"),
        "avg_hr": find_col(header, "average", "hr") or find_col(header, "average", "heart"),
        "max_hr": find_col(header, "max", "hr") or find_col(header, "max", "heart"),
        "dist": find_col(header, "distance"),
    }
    n = 0
    for i, row in enumerate(rows[1:]):
        def val(key):
            j = cols[key]
            return to_float(row[j]) if j is not None and j < len(row) else None

        day = parse_day(row[c_start])
        if not day:
            continue
        kcal = val("kcal")
        conn.execute(
            "INSERT INTO workouts (id, day, start_ts, end_ts, sport_name, score_state, "
            "strain, average_heart_rate, max_heart_rate, kilojoule, distance_meter, raw) "
            "VALUES (?,?,?,?,?,'SCORED',?,?,?,?,?,'{\"source\":\"export\"}') "
            "ON CONFLICT(id) DO NOTHING",
            (f"export-{day}-{i}", day, row[c_start],
             row[c_end] if c_end is not None and c_end < len(row) else None,
             row[c_sport].strip() if c_sport is not None and c_sport < len(row) else None,
             val("strain"), val("avg_hr"), val("max_hr"),
             kcal * 4.184 if kcal else None, val("dist")),
        )
        n += 1
    return n


ROUTES = [
    (("journal",), import_journal, "journal entries"),
    (("physiological", "cycle"), import_cycles, "daily cycles"),
    (("sleep",), import_sleeps, "sleeps"),
    (("workout",), import_workouts, "workouts"),
]


def main():
    ap = argparse.ArgumentParser(description="Import a WHOOP data export.")
    ap.add_argument("path", help="path to the export .zip, folder, or a single .csv")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        raise SystemExit(f"No database at {DB_PATH}. Run whoop_sync.py --auth first.")

    conn = sqlite3.connect(DB_PATH)
    totals = {}

    for name, rows in iter_csvs(args.path):
        if len(rows) < 2:
            continue
        lowered = norm(name)
        for keywords, importer, label in ROUTES:
            if any(k in lowered for k in keywords):
                try:
                    count = importer(conn, rows)
                except (sqlite3.Error, IndexError, ValueError) as exc:
                    print(f"  ! {name}: {exc}", file=sys.stderr)
                    count = 0
                totals[label] = totals.get(label, 0) + count
                print(f"  {name}: {count} {label}")
                break
        else:
            print(f"  {name}: skipped (unrecognised)")

    conn.commit()
    if not totals:
        raise SystemExit("Nothing imported — is this really a WHOOP export?")
    print("\nImported: " + ", ".join(f"{v} {k}" for k, v in totals.items()))
    print("Now run:  python3 whoop_insights.py --weekly --print")


if __name__ == "__main__":
    main()
