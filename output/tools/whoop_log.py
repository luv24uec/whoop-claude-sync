#!/usr/bin/env python3
"""
whoop_log.py — log the things WHOOP can't measure.

WHOOP tracks recovery, sleep, and strain. It does not know your scale weight,
your body-fat percentage, your waist, or what you ate. Those are the numbers
that actually decide whether you reach 10% body fat, so they live in the same
database and get analysed alongside everything else.

  python3 whoop_log.py weight 78.4
  python3 whoop_log.py body_fat 18.2
  python3 whoop_log.py waist 84 --date 2026-07-30
  python3 whoop_log.py calories 2100 --note "high carb day"
  python3 whoop_log.py protein 180
  python3 whoop_log.py --list weight
"""

import argparse
import os
import sqlite3
from datetime import date

DATA_DIR = os.path.expanduser(os.environ.get("WHOOP_DATA_DIR", "~/WhoopData"))
DB_PATH = os.path.join(DATA_DIR, "whoop.db")

ALIASES = {
    "weight": "weight_kg", "kg": "weight_kg", "weight_kg": "weight_kg",
    "bf": "body_fat_pct", "body_fat": "body_fat_pct", "bodyfat": "body_fat_pct",
    "body_fat_pct": "body_fat_pct",
    "waist": "waist_cm", "waist_cm": "waist_cm",
    "calories": "intake_kcal", "kcal": "intake_kcal", "intake_kcal": "intake_kcal",
    "protein": "protein_g", "protein_g": "protein_g",
    "steps": "steps",
}

SANITY = {
    "weight_kg": (30, 250, "kg"),
    "body_fat_pct": (3, 60, "%"),
    "waist_cm": (40, 200, "cm"),
    "intake_kcal": (500, 8000, "kcal"),
    "protein_g": (10, 500, "g"),
    "steps": (0, 100000, "steps"),
}


def main():
    ap = argparse.ArgumentParser(description="Log a manual health metric.")
    ap.add_argument("metric", nargs="?", help="weight | body_fat | waist | calories | protein | steps")
    ap.add_argument("value", nargs="?", type=float)
    ap.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD (default: today)")
    ap.add_argument("--note", default="")
    ap.add_argument("--list", metavar="METRIC", help="print the stored history for a metric")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        raise SystemExit(f"No database at {DB_PATH}. Run whoop_sync.py --auth first.")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS manual_log (
        day TEXT, metric TEXT, value REAL, note TEXT, PRIMARY KEY (day, metric))""")

    if args.list:
        key = ALIASES.get(args.list, args.list)
        rows = conn.execute(
            "SELECT day, value, note FROM manual_log WHERE metric=? ORDER BY day", (key,)
        ).fetchall()
        if not rows:
            print(f"No entries for {key}.")
            return
        print(f"\n{key}\n" + "-" * 34)
        for day, value, note in rows:
            print(f"  {day}   {value:>8.1f}   {note}")
        print()
        return

    if not args.metric or args.value is None:
        ap.error("give a metric and a value, e.g. `whoop_log.py weight 78.4`")

    key = ALIASES.get(args.metric.lower())
    if not key:
        raise SystemExit(f"Unknown metric '{args.metric}'. Known: {', '.join(sorted(set(ALIASES)))}")

    lo, hi, unit = SANITY[key]
    if not lo <= args.value <= hi:
        raise SystemExit(f"{args.value} {unit} is outside the plausible range "
                         f"({lo}–{hi} {unit}). Typo?")

    conn.execute(
        "INSERT INTO manual_log (day, metric, value, note) VALUES (?,?,?,?) "
        "ON CONFLICT(day, metric) DO UPDATE SET value=excluded.value, note=excluded.note",
        (args.date, key, args.value, args.note),
    )
    conn.commit()
    print(f"Logged {key} = {args.value} {unit} for {args.date}")


if __name__ == "__main__":
    main()
