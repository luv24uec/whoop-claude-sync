"""Fetch Whoop data, merge archives, render markdown."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from whoop_claude_sync.client import WhoopClient
from whoop_claude_sync.config import Settings
from whoop_claude_sync.render import render_all
from whoop_claude_sync.store import (
    COLLECTIONS,
    load_collection,
    merge_records,
    read_sync_meta,
    utc_now_iso,
    write_sync_meta,
)


def ensure_out_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "archive").mkdir(parents=True, exist_ok=True)


def resolve_window(settings: Settings, days: int | None = None) -> tuple[datetime, datetime, bool]:
    """Return (start, end, is_backfill)."""
    end = datetime.now(timezone.utc)
    meta = read_sync_meta(settings.out_dir)
    has_data = any(load_collection(settings.out_dir, name) for name in COLLECTIONS)
    if days is not None:
        return end - timedelta(days=days), end, not has_data
    if not has_data or not meta:
        return end - timedelta(days=settings.backfill_days), end, True
    return end - timedelta(days=settings.sync_days), end, False


def run_sync(settings: Settings, days: int | None = None) -> dict[str, Any]:
    ensure_out_dir(settings.out_dir)
    start, end, is_backfill = resolve_window(settings, days=days)
    counts: dict[str, dict[str, int]] = {}
    profile: dict[str, Any] | None = None

    with WhoopClient(settings) as client:
        try:
            profile = client.get_profile()
        except Exception as exc:  # noqa: BLE001 — keep sync going without profile
            profile = {"_error": str(exc)}

        mapping = {
            "recovery": client.iter_recoveries,
            "sleep": client.iter_sleeps,
            "cycles": client.iter_cycles,
            "workouts": client.iter_workouts,
        }
        for name, iterator in mapping.items():
            records = list(iterator(start=start, end=end))
            upserted, total = merge_records(settings.out_dir, name, records)
            counts[name] = {
                "fetched": len(records),
                "upserted": upserted,
                "total": total,
            }

    written = render_all(settings.out_dir, profile=profile if profile and "_error" not in profile else None)
    meta = {
        "last_sync_at": utc_now_iso(),
        "window_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backfill": is_backfill,
        "counts": counts,
        "profile": {
            "first_name": (profile or {}).get("first_name"),
            "last_name": (profile or {}).get("last_name"),
            "user_id": (profile or {}).get("user_id"),
        }
        if profile and "_error" not in profile
        else None,
        "files": written,
    }
    write_sync_meta(settings.out_dir, meta)

    empty = all(c["total"] == 0 for c in counts.values())
    if empty:
        meta["warning"] = (
            "No Whoop records returned. Confirm an active Whoop membership "
            "and that scopes include recovery/sleep/cycles/workout."
        )
    return meta
