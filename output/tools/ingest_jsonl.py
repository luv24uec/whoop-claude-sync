#!/usr/bin/env python3
"""
ingest_jsonl.py — load raw WHOOP v2 JSONL archives into the analysis database.

The Cursor-built sync writes raw API records to cycles/recovery/sleep/workouts
.jsonl. That is the same shape whoop_sync.py stores, so this just reuses the
same flatteners and upserts — no data is reinterpreted.

  python3 ingest_jsonl.py /path/to/Whoop/archive
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whoop_sync as ws  # noqa: E402


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def main(archive_dir):
    conn = ws.connect()
    counts = {}

    cycles = read_jsonl(os.path.join(archive_dir, "cycles.jsonl"))
    day_lookup = {}
    for rec in cycles:
        row = ws.flat_cycle(rec)
        day_lookup[row["id"]] = row["day"]
        ws.upsert(conn, "cycles", row)
    counts["cycles"] = len(cycles)

    recoveries = read_jsonl(os.path.join(archive_dir, "recovery.jsonl"))
    for rec in recoveries:
        ws.upsert(conn, "recovery", ws.flat_recovery(rec, day_lookup))
    counts["recovery"] = len(recoveries)

    sleeps = read_jsonl(os.path.join(archive_dir, "sleep.jsonl"))
    for rec in sleeps:
        ws.upsert(conn, "sleep", ws.flat_sleep(rec))
    counts["sleep"] = len(sleeps)

    workouts = read_jsonl(os.path.join(archive_dir, "workouts.jsonl"))
    for rec in workouts:
        ws.upsert(conn, "workouts", ws.flat_workout(rec))
    counts["workouts"] = len(workouts)

    conn.commit()
    ws.realign_days(conn)
    ws.export_csv(conn)
    print("Ingested: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    ws.show_status(conn)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
