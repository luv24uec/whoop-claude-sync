"""JSONL archive merge + sync metadata."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


COLLECTIONS = ("recovery", "sleep", "cycles", "workouts")


def archive_dir(out_dir: Path) -> Path:
    return out_dir / "archive"


def collection_path(out_dir: Path, name: str) -> Path:
    return archive_dir(out_dir) / f"{name}.jsonl"


def record_id(name: str, record: dict[str, Any]) -> str | None:
    if name == "recovery":
        return _as_str(record.get("cycle_id") or record.get("sleep_id"))
    return _as_str(record.get("id"))


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def load_collection(out_dir: Path, name: str) -> dict[str, dict[str, Any]]:
    path = collection_path(out_dir, name)
    by_id: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return by_id
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        rid = record_id(name, record)
        if rid:
            by_id[rid] = record
    return by_id


def merge_records(
    out_dir: Path,
    name: str,
    records: Iterable[dict[str, Any]],
) -> tuple[int, int]:
    """Merge records into JSONL. Returns (upserted, total)."""
    existing = load_collection(out_dir, name)
    upserted = 0
    for record in records:
        rid = record_id(name, record)
        if not rid:
            continue
        prev = existing.get(rid)
        if prev != record:
            existing[rid] = record
            upserted += 1
    write_collection(out_dir, name, existing.values())
    return upserted, len(existing)


def write_collection(out_dir: Path, name: str, records: Iterable[dict[str, Any]]) -> None:
    path = collection_path(out_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sort newest-first when timestamps exist.
    items = list(records)

    def sort_key(r: dict[str, Any]) -> str:
        return str(r.get("start") or r.get("created_at") or r.get("updated_at") or "")

    items.sort(key=sort_key, reverse=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for record in items:
            f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")
    tmp.replace(path)


def write_sync_meta(out_dir: Path, meta: dict[str, Any]) -> None:
    archive_dir(out_dir).mkdir(parents=True, exist_ok=True)
    path = archive_dir(out_dir) / "sync_meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_sync_meta(out_dir: Path) -> dict[str, Any] | None:
    path = archive_dir(out_dir) / "sync_meta.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
