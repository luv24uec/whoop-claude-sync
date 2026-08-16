#!/usr/bin/env python3
"""Export Whoop CSVs into named Excel workbooks under ~/Whoop (stdlib only)."""

from __future__ import annotations

import csv
import datetime as dt
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

CSV_DIR = Path.home() / "ClaudeProjects" / "Whoop" / "csv"
OUT_DIR = Path.home() / "Whoop"
KJ_PER_KCAL = 4.184

# Excel serial epoch
_EXCEL_EPOCH = dt.datetime(1899, 12, 30)


def read_csv(name: str) -> list[dict]:
    path = CSV_DIR / name
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def milli_to_hours(v):
    f = to_float(v)
    return None if f is None else round(f / 3_600_000, 2)


def milli_to_minutes(v):
    f = to_float(v)
    return None if f is None else round(f / 60_000, 1)


def kj_to_kcal(v):
    f = to_float(v)
    return None if f is None else round(f / KJ_PER_KCAL, 1)


def _col_name(idx: int) -> str:
    """1-based column index → A, B, … AA."""
    name = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell_xml(ref: str, value) -> str:
    if value is None or value == "":
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value:  # NaN
            return f'<c r="{ref}"/>'
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _sheet_xml(headers: list[str], rows: list[dict]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    header_cells = "".join(
        _cell_xml(f"{_col_name(i)}1", h) for i, h in enumerate(headers, 1)
    )
    lines.append(f'<row r="1">{header_cells}</row>')
    for r_idx, row in enumerate(rows, 2):
        body = "".join(
            _cell_xml(f"{_col_name(c_idx)}{r_idx}", row.get(h))
            for c_idx, h in enumerate(headers, 1)
        )
        lines.append(f'<row r="{r_idx}">{body}</row>')
    lines.append("</sheetData></worksheet>")
    return "\n".join(lines)


def save_workbook(path: Path, sheets: dict[str, tuple[list[str], list[dict]]]) -> None:
    """Write a minimal .xlsx (OOXML) with one or more sheets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_names = list(sheets.keys())
    content_types = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]
    for i in range(1, len(sheet_names) + 1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append("</Types>")

    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    wb_rels_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    sheets_xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        "<sheets>",
    ]
    for i, name in enumerate(sheet_names, 1):
        safe = re.sub(r"[\[\]\*\/\\?:]", "_", name)[:31] or f"Sheet{i}"
        sheets_xml.append(f'<sheet name="{escape(safe)}" sheetId="{i}" r:id="rId{i}"/>')
        wb_rels_parts.append(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )
    sheets_xml.append("</sheets></workbook>")
    wb_rels_parts.append("</Relationships>")

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "\n".join(content_types))
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", "\n".join(sheets_xml))
        zf.writestr("xl/_rels/workbook.xml.rels", "\n".join(wb_rels_parts))
        for i, name in enumerate(sheet_names, 1):
            headers, rows = sheets[name]
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(headers, rows))

    print(f"Wrote {path} ({sum(len(r) for _, r in sheets.values())} rows)")


def main() -> int:
    if not CSV_DIR.exists():
        print(f"No CSV data at {CSV_DIR}", file=sys.stderr)
        return 1

    recovery = read_csv("recovery.csv")
    sleep = read_csv("sleep.csv")
    cycles = read_csv("cycles.csv")
    workouts = read_csv("workouts.csv")

    sleep_by_day: dict[str, dict] = {}
    for s in sleep:
        if str(s.get("nap", "0")) in ("1", "True", "true"):
            continue
        day = s.get("day")
        if not day:
            continue
        prev = sleep_by_day.get(day)
        if prev is None or (to_float(s.get("total_in_bed_milli")) or 0) > (
            to_float(prev.get("total_in_bed_milli")) or 0
        ):
            sleep_by_day[day] = s

    rec_by_day = {r["day"]: r for r in recovery if r.get("day")}
    cyc_by_day = {c["day"]: c for c in cycles if c.get("day")}

    rec_headers = [
        "day",
        "recovery_score",
        "hrv_rmssd_ms",
        "resting_heart_rate_bpm",
        "spo2_pct",
        "skin_temp_c",
        "score_state",
    ]
    rec_rows = []
    for r in sorted(recovery, key=lambda x: x.get("day") or ""):
        hrv = to_float(r.get("hrv_rmssd_milli"))
        spo2 = to_float(r.get("spo2_percentage"))
        skin = to_float(r.get("skin_temp_celsius"))
        rec_rows.append(
            {
                "day": r.get("day"),
                "recovery_score": to_float(r.get("recovery_score")),
                "hrv_rmssd_ms": round(hrv, 1) if hrv is not None else None,
                "resting_heart_rate_bpm": to_float(r.get("resting_heart_rate")),
                "spo2_pct": round(spo2, 1) if spo2 is not None else None,
                "skin_temp_c": round(skin, 2) if skin is not None else None,
                "score_state": r.get("score_state"),
            }
        )

    sleep_headers = [
        "day",
        "nap",
        "asleep_hours",
        "in_bed_hours",
        "awake_minutes",
        "light_hours",
        "deep_sws_hours",
        "rem_hours",
        "sleep_performance_pct",
        "sleep_efficiency_pct",
        "sleep_consistency_pct",
        "respiratory_rate",
        "disturbance_count",
        "sleep_cycles",
        "sleep_need_hours",
        "score_state",
    ]
    sleep_rows = []
    for s in sorted(sleep, key=lambda x: x.get("day") or ""):
        need = (
            (to_float(s.get("need_baseline_milli")) or 0)
            + (to_float(s.get("need_from_debt_milli")) or 0)
            + (to_float(s.get("need_from_strain_milli")) or 0)
            - (to_float(s.get("need_from_nap_milli")) or 0)
        )
        asleep = (
            (to_float(s.get("total_light_milli")) or 0)
            + (to_float(s.get("total_sws_milli")) or 0)
            + (to_float(s.get("total_rem_milli")) or 0)
        )
        eff = to_float(s.get("sleep_efficiency_pct"))
        resp = to_float(s.get("respiratory_rate"))
        sleep_rows.append(
            {
                "day": s.get("day"),
                "nap": "yes" if str(s.get("nap", "0")) in ("1", "True", "true") else "no",
                "asleep_hours": round(asleep / 3_600_000, 2),
                "in_bed_hours": milli_to_hours(s.get("total_in_bed_milli")),
                "awake_minutes": milli_to_minutes(s.get("total_awake_milli")),
                "light_hours": milli_to_hours(s.get("total_light_milli")),
                "deep_sws_hours": milli_to_hours(s.get("total_sws_milli")),
                "rem_hours": milli_to_hours(s.get("total_rem_milli")),
                "sleep_performance_pct": to_float(s.get("sleep_performance_pct")),
                "sleep_efficiency_pct": round(eff, 1) if eff is not None else None,
                "sleep_consistency_pct": to_float(s.get("sleep_consistency_pct")),
                "respiratory_rate": round(resp, 2) if resp is not None else None,
                "disturbance_count": to_float(s.get("disturbance_count")),
                "sleep_cycles": to_float(s.get("sleep_cycle_count")),
                "sleep_need_hours": round(need / 3_600_000, 2) if need else None,
                "score_state": s.get("score_state"),
            }
        )

    cyc_headers = [
        "day",
        "strain",
        "calories_kcal",
        "avg_heart_rate_bpm",
        "max_heart_rate_bpm",
        "score_state",
    ]
    cyc_rows = []
    for c in sorted(cycles, key=lambda x: x.get("day") or ""):
        strain = to_float(c.get("strain"))
        cyc_rows.append(
            {
                "day": c.get("day"),
                "strain": round(strain, 2) if strain is not None else None,
                "calories_kcal": kj_to_kcal(c.get("kilojoule")),
                "avg_heart_rate_bpm": to_float(c.get("average_heart_rate")),
                "max_heart_rate_bpm": to_float(c.get("max_heart_rate")),
                "score_state": c.get("score_state"),
            }
        )

    wo_headers = [
        "day",
        "sport",
        "strain",
        "calories_kcal",
        "avg_hr_bpm",
        "max_hr_bpm",
        "duration_minutes",
        "distance_km",
        "zone0_min",
        "zone1_min",
        "zone2_min",
        "zone3_min",
        "zone4_min",
        "zone5_min",
        "score_state",
    ]
    wo_rows = []
    for w in sorted(workouts, key=lambda x: (x.get("day") or "", x.get("start_ts") or "")):
        start, end = w.get("start_ts"), w.get("end_ts")
        duration = None
        if start and end:
            try:
                a = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
                b = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
                duration = round((b - a).total_seconds() / 60, 1)
            except ValueError:
                pass
        dist = to_float(w.get("distance_meter"))
        strain = to_float(w.get("strain"))
        wo_rows.append(
            {
                "day": w.get("day"),
                "sport": w.get("sport_name"),
                "strain": round(strain, 2) if strain is not None else None,
                "calories_kcal": kj_to_kcal(w.get("kilojoule")),
                "avg_hr_bpm": to_float(w.get("average_heart_rate")),
                "max_hr_bpm": to_float(w.get("max_heart_rate")),
                "duration_minutes": duration,
                "distance_km": round(dist / 1000, 2) if dist else None,
                "zone0_min": milli_to_minutes(w.get("zone_zero_milli")),
                "zone1_min": milli_to_minutes(w.get("zone_one_milli")),
                "zone2_min": milli_to_minutes(w.get("zone_two_milli")),
                "zone3_min": milli_to_minutes(w.get("zone_three_milli")),
                "zone4_min": milli_to_minutes(w.get("zone_four_milli")),
                "zone5_min": milli_to_minutes(w.get("zone_five_milli")),
                "score_state": w.get("score_state"),
            }
        )

    days = sorted(set(rec_by_day) | set(sleep_by_day) | set(cyc_by_day))
    daily_headers = [
        "day",
        "recovery_score",
        "hrv_rmssd_ms",
        "resting_heart_rate_bpm",
        "spo2_pct",
        "skin_temp_c",
        "asleep_hours",
        "in_bed_hours",
        "sleep_performance_pct",
        "sleep_efficiency_pct",
        "sleep_consistency_pct",
        "rem_hours",
        "deep_sws_hours",
        "sleep_need_hours",
        "day_strain",
        "day_calories_kcal",
        "day_avg_hr_bpm",
        "day_max_hr_bpm",
        "workout_count",
        "workout_strain_sum",
    ]
    wo_count: dict[str, int] = defaultdict(int)
    wo_strain: dict[str, float] = defaultdict(float)
    for w in workouts:
        d = w.get("day")
        if not d:
            continue
        wo_count[d] += 1
        wo_strain[d] += to_float(w.get("strain")) or 0

    daily_rows = []
    for day in days:
        r = rec_by_day.get(day, {})
        s = sleep_by_day.get(day, {})
        c = cyc_by_day.get(day, {})
        asleep = None
        need = None
        if s:
            asleep = round(
                (
                    (to_float(s.get("total_light_milli")) or 0)
                    + (to_float(s.get("total_sws_milli")) or 0)
                    + (to_float(s.get("total_rem_milli")) or 0)
                )
                / 3_600_000,
                2,
            )
            need_ms = (
                (to_float(s.get("need_baseline_milli")) or 0)
                + (to_float(s.get("need_from_debt_milli")) or 0)
                + (to_float(s.get("need_from_strain_milli")) or 0)
                - (to_float(s.get("need_from_nap_milli")) or 0)
            )
            need = round(need_ms / 3_600_000, 2) if need_ms else None
        hrv = to_float(r.get("hrv_rmssd_milli"))
        spo2 = to_float(r.get("spo2_percentage"))
        skin = to_float(r.get("skin_temp_celsius"))
        strain = to_float(c.get("strain"))
        eff = to_float(s.get("sleep_efficiency_pct")) if s else None
        daily_rows.append(
            {
                "day": day,
                "recovery_score": to_float(r.get("recovery_score")),
                "hrv_rmssd_ms": round(hrv, 1) if hrv is not None else None,
                "resting_heart_rate_bpm": to_float(r.get("resting_heart_rate")),
                "spo2_pct": round(spo2, 1) if spo2 is not None else None,
                "skin_temp_c": round(skin, 2) if skin is not None else None,
                "asleep_hours": asleep,
                "in_bed_hours": milli_to_hours(s.get("total_in_bed_milli")) if s else None,
                "sleep_performance_pct": to_float(s.get("sleep_performance_pct")) if s else None,
                "sleep_efficiency_pct": round(eff, 1) if eff is not None else None,
                "sleep_consistency_pct": to_float(s.get("sleep_consistency_pct")) if s else None,
                "rem_hours": milli_to_hours(s.get("total_rem_milli")) if s else None,
                "deep_sws_hours": milli_to_hours(s.get("total_sws_milli")) if s else None,
                "sleep_need_hours": need,
                "day_strain": round(strain, 2) if strain is not None else None,
                "day_calories_kcal": kj_to_kcal(c.get("kilojoule")) if c else None,
                "day_avg_hr_bpm": to_float(c.get("average_heart_rate")) if c else None,
                "day_max_hr_bpm": to_float(c.get("max_heart_rate")) if c else None,
                "workout_count": wo_count.get(day, 0),
                "workout_strain_sum": round(wo_strain.get(day, 0), 2) if wo_count.get(day) else 0,
            }
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    save_workbook(OUT_DIR / "Whoop_Recovery.xlsx", {"Recovery": (rec_headers, rec_rows)})
    save_workbook(OUT_DIR / "Whoop_Sleep.xlsx", {"Sleep": (sleep_headers, sleep_rows)})
    save_workbook(OUT_DIR / "Whoop_Strain.xlsx", {"Strain": (cyc_headers, cyc_rows)})
    save_workbook(OUT_DIR / "Whoop_Workouts.xlsx", {"Workouts": (wo_headers, wo_rows)})
    save_workbook(OUT_DIR / "Whoop_Daily.xlsx", {"Daily": (daily_headers, daily_rows)})
    save_workbook(
        OUT_DIR / "Whoop_All_Metrics.xlsx",
        {
            "Daily": (daily_headers, daily_rows),
            "Recovery": (rec_headers, rec_rows),
            "Sleep": (sleep_headers, sleep_rows),
            "Strain": (cyc_headers, cyc_rows),
            "Workouts": (wo_headers, wo_rows),
        },
    )

    (OUT_DIR / "README.txt").write_text(
        f"""Whoop Excel exports
Generated: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
Source: {CSV_DIR}

Files:
  Whoop_All_Metrics.xlsx  — all sheets in one workbook (start here)
  Whoop_Daily.xlsx         — one row per day
  Whoop_Recovery.xlsx
  Whoop_Sleep.xlsx
  Whoop_Strain.xlsx
  Whoop_Workouts.xlsx
""",
        encoding="utf-8",
    )
    print(f"Done → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
