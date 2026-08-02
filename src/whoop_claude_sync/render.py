"""Render Claude-friendly markdown from Whoop archives."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from whoop_claude_sync.store import COLLECTIONS, load_collection, utc_now_iso


PROJECT_INSTRUCTIONS = """You are my personal Whoop performance coach.

Use WHOOP_BRIEF.md and the dated summaries as the source of truth for metrics.
Prefer trends and actionable recovery advice over generic wellness tips.
If synced data is older than 36 hours, say so and ask me to re-sync.
Never invent HRV, recovery, sleep, or strain numbers — only cite synced files.
When recommending training intensity, weight recovery and sleep more than motivation.
"""


def render_all(
    out_dir,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    data = {name: list(load_collection(out_dir, name).values()) for name in COLLECTIONS}
    written = []

    brief = render_brief(data, profile=profile)
    path = out_dir / "WHOOP_BRIEF.md"
    path.write_text(brief, encoding="utf-8")
    written.append(str(path))

    week = render_window(data, days=7, title="WHOOP — Last 7 Days")
    path = out_dir / "WHOOP_LAST_7_DAYS.md"
    path.write_text(week, encoding="utf-8")
    written.append(str(path))

    month = render_window(data, days=30, title="WHOOP — Last 30 Days")
    path = out_dir / "WHOOP_LAST_30_DAYS.md"
    path.write_text(month, encoding="utf-8")
    written.append(str(path))

    workouts = render_workouts(data.get("workouts") or [])
    path = out_dir / "WHOOP_WORKOUTS.md"
    path.write_text(workouts, encoding="utf-8")
    written.append(str(path))

    instr = out_dir / "PROJECT_INSTRUCTIONS.md"
    if not instr.exists():
        instr.write_text(PROJECT_INSTRUCTIONS, encoding="utf-8")
        written.append(str(instr))

    return written


def render_brief(data: dict[str, list[dict]], profile: dict | None = None) -> str:
    meta = None
    recoveries = _sorted(data.get("recovery") or [], "created_at")
    sleeps = _sorted(data.get("sleep") or [], "start")
    cycles = _sorted(data.get("cycles") or [], "start")
    workouts = _sorted(data.get("workouts") or [], "start")

    latest_rec = recoveries[0] if recoveries else None
    latest_sleep = _latest_primary_sleep(sleeps)
    latest_cycle = cycles[0] if cycles else None

    lines = [
        "# WHOOP Brief",
        "",
        f"_Generated: {utc_now_iso()}_",
        "",
    ]
    if profile:
        name = " ".join(
            x for x in [profile.get("first_name"), profile.get("last_name")] if x
        ).strip()
        if name:
            lines.append(f"**Athlete:** {name}")
        if profile.get("email"):
            lines.append(f"**Email:** {profile['email']}")
        lines.append("")

    lines.extend(["## Latest snapshot", ""])
    if latest_rec:
        score = (latest_rec.get("score") or {})
        lines.append(
            f"- **Recovery:** {_fmt(score.get('recovery_score'))}% "
            f"({_bucket_recovery(score.get('recovery_score'))})"
        )
        lines.append(f"- **HRV (rmssd):** {_fmt(score.get('hrv_rmssd_milli'), suffix=' ms')}")
        lines.append(f"- **Resting HR:** {_fmt(score.get('resting_heart_rate'), suffix=' bpm')}")
        if score.get("spo2_percentage") is not None:
            lines.append(f"- **SpO2:** {_fmt(score.get('spo2_percentage'), suffix='%')}")
        if score.get("skin_temp_celsius") is not None:
            lines.append(f"- **Skin temp:** {_fmt(score.get('skin_temp_celsius'), suffix=' °C')}")
    else:
        lines.append("- No recovery records yet.")

    if latest_sleep:
        s = latest_sleep.get("score") or {}
        stage = s.get("stage_summary") or {}
        lines.append(
            f"- **Sleep performance:** {_fmt(s.get('sleep_performance_percentage'), suffix='%')}"
        )
        lines.append(
            f"- **Sleep duration:** {_ms_to_hhmm(stage.get('total_in_bed_time_milli'))} in bed "
            f"/ {_ms_to_hhmm(_asleep_ms(stage))} asleep"
        )
        lines.append(
            f"- **Sleep efficiency:** {_fmt(s.get('sleep_efficiency_percentage'), suffix='%')}"
        )
    if latest_cycle:
        cscore = latest_cycle.get("score") or {}
        lines.append(f"- **Day strain:** {_fmt(cscore.get('strain'))}")
        lines.append(
            f"- **Day avg HR:** {_fmt(cscore.get('average_heart_rate'), suffix=' bpm')}"
        )

    lines.extend(["", "## Trends", ""])
    lines.extend(_trend_block(recoveries, sleeps, cycles, days=7, label="7-day"))
    lines.extend(_trend_block(recoveries, sleeps, cycles, days=30, label="30-day"))

    lines.extend(["", "## Notable signals", ""])
    signals = _signals(recoveries, sleeps, cycles, workouts)
    if signals:
        lines.extend(f"- {s}" for s in signals)
    else:
        lines.append("- No strong outliers in the recent window.")

    lines.extend(
        [
            "",
            "## Files in this Project",
            "",
            "- `WHOOP_BRIEF.md` — this executive snapshot",
            "- `WHOOP_LAST_7_DAYS.md` — day-by-day last week",
            "- `WHOOP_LAST_30_DAYS.md` — day-by-day last month",
            "- `WHOOP_WORKOUTS.md` — recent workouts",
            "- `PROJECT_INSTRUCTIONS.md` — suggested custom instructions",
            "",
        ]
    )
    return "\n".join(lines)


def render_window(data: dict[str, list[dict]], days: int, title: str) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recoveries = _filter_since(data.get("recovery") or [], "created_at", cutoff)
    sleeps = _filter_since(data.get("sleep") or [], "start", cutoff)
    cycles = _filter_since(data.get("cycles") or [], "start", cutoff)

    by_day: dict[str, dict[str, Any]] = {}

    for r in recoveries:
        day = _day_key(r.get("created_at") or r.get("updated_at"))
        if day:
            by_day.setdefault(day, {})["recovery"] = r
    for s in sleeps:
        if s.get("nap"):
            continue
        day = _day_key(s.get("end") or s.get("start"))
        if day:
            by_day.setdefault(day, {})["sleep"] = s
    for c in cycles:
        day = _day_key(c.get("start"))
        if day:
            by_day.setdefault(day, {})["cycle"] = c

    lines = [f"# {title}", "", f"_Generated: {utc_now_iso()}_", ""]
    lines.append("| Day | Recovery | HRV | RHR | Sleep perf | Strain |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for day in sorted(by_day.keys(), reverse=True):
        row = by_day[day]
        rec = (row.get("recovery") or {}).get("score") or {}
        sleep = (row.get("sleep") or {}).get("score") or {}
        cycle = (row.get("cycle") or {}).get("score") or {}
        lines.append(
            "| {day} | {rec} | {hrv} | {rhr} | {sleep} | {strain} |".format(
                day=day,
                rec=_fmt(rec.get("recovery_score")),
                hrv=_fmt(rec.get("hrv_rmssd_milli")),
                rhr=_fmt(rec.get("resting_heart_rate")),
                sleep=_fmt(sleep.get("sleep_performance_percentage")),
                strain=_fmt(cycle.get("strain"), digits=1),
            )
        )
    lines.append("")
    return "\n".join(lines)


def render_workouts(workouts: list[dict], limit: int = 40) -> str:
    items = _sorted(workouts, "start")[:limit]
    lines = [
        "# WHOOP Workouts",
        "",
        f"_Generated: {utc_now_iso()}_",
        "",
        "| When | Sport | Strain | Avg HR | Max HR | Duration |",
        "|---|---|---:|---:|---:|---|",
    ]
    for w in items:
        score = w.get("score") or {}
        lines.append(
            "| {when} | {sport} | {strain} | {avg} | {mx} | {dur} |".format(
                when=_day_key(w.get("start")) or "?",
                sport=w.get("sport_name") or w.get("sport_id") or "?",
                strain=_fmt(score.get("strain"), digits=1),
                avg=_fmt(score.get("average_heart_rate")),
                mx=_fmt(score.get("max_heart_rate")),
                dur=_duration(w.get("start"), w.get("end")),
            )
        )
    if not items:
        lines.append("| — | No workouts in archive | — | — | — | — |")
    lines.append("")
    return "\n".join(lines)


def _trend_block(recoveries, sleeps, cycles, days: int, label: str) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recs = _filter_since(recoveries, "created_at", cutoff)
    slp = [s for s in _filter_since(sleeps, "start", cutoff) if not s.get("nap")]
    cyc = _filter_since(cycles, "start", cutoff)

    rec_scores = [
        (r.get("score") or {}).get("recovery_score")
        for r in recs
        if (r.get("score") or {}).get("recovery_score") is not None
    ]
    hrv = [
        (r.get("score") or {}).get("hrv_rmssd_milli")
        for r in recs
        if (r.get("score") or {}).get("hrv_rmssd_milli") is not None
    ]
    sleep_perf = [
        (s.get("score") or {}).get("sleep_performance_percentage")
        for s in slp
        if (s.get("score") or {}).get("sleep_performance_percentage") is not None
    ]
    strain = [
        (c.get("score") or {}).get("strain")
        for c in cyc
        if (c.get("score") or {}).get("strain") is not None
    ]

    return [
        f"- **{label} avg recovery:** {_fmt(_safe_mean(rec_scores), suffix='%')} "
        f"(n={len(rec_scores)})",
        f"- **{label} avg HRV:** {_fmt(_safe_mean(hrv), suffix=' ms')} (n={len(hrv)})",
        f"- **{label} avg sleep performance:** {_fmt(_safe_mean(sleep_perf), suffix='%')} "
        f"(n={len(sleep_perf)})",
        f"- **{label} avg strain:** {_fmt(_safe_mean(strain), digits=1)} (n={len(strain)})",
    ]


def _signals(recoveries, sleeps, cycles, workouts) -> list[str]:
    signals: list[str] = []
    cutoff7 = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff30 = datetime.now(timezone.utc) - timedelta(days=30)
    hrv7 = _values(_filter_since(recoveries, "created_at", cutoff7), "hrv_rmssd_milli")
    hrv30 = _values(_filter_since(recoveries, "created_at", cutoff30), "hrv_rmssd_milli")
    if hrv7 and hrv30:
        avg7, avg30 = mean(hrv7), mean(hrv30)
        if avg30:
            delta = (avg7 - avg30) / avg30 * 100
            if abs(delta) >= 10:
                direction = "up" if delta > 0 else "down"
                signals.append(f"HRV {direction} {abs(delta):.0f}% vs 30-day average")

    rec7 = _values(_filter_since(recoveries, "created_at", cutoff7), "recovery_score")
    low_days = sum(1 for x in rec7 if x is not None and x < 34)
    if low_days >= 2:
        signals.append(f"{low_days} red recovery days in the last week")

    recent_sleeps = [
        s for s in _filter_since(sleeps, "start", cutoff7) if not s.get("nap")
    ]
    short = 0
    for s in recent_sleeps:
        stage = (s.get("score") or {}).get("stage_summary") or {}
        asleep = _asleep_ms(stage)
        if asleep is not None and asleep < 6 * 3600 * 1000:
            short += 1
    if short >= 2:
        signals.append(f"{short} nights under 6h asleep in the last week")

    hard = [
        w
        for w in _filter_since(workouts, "start", cutoff7)
        if ((w.get("score") or {}).get("strain") or 0) >= 14
    ]
    if hard:
        signals.append(f"{len(hard)} high-strain workouts (≥14) in the last week")
    return signals


def _values(records: list[dict], score_key: str) -> list[float]:
    out = []
    for r in records:
        v = (r.get("score") or {}).get(score_key)
        if v is not None:
            out.append(float(v))
    return out


def _sorted(records: list[dict], key: str) -> list[dict]:
    return sorted(records, key=lambda r: str(r.get(key) or ""), reverse=True)


def _filter_since(records: list[dict], key: str, cutoff: datetime) -> list[dict]:
    out = []
    for r in records:
        dt = _parse_dt(r.get(key))
        if dt and dt >= cutoff:
            out.append(r)
    return out


def _latest_primary_sleep(sleeps: list[dict]) -> dict | None:
    for s in sleeps:
        if not s.get("nap"):
            return s
    return sleeps[0] if sleeps else None


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day_key(value: Any) -> str | None:
    dt = _parse_dt(value)
    if not dt:
        return None
    return dt.astimezone().strftime("%Y-%m-%d")


def _fmt(value: Any, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits == 0:
        text = str(int(round(num)))
    else:
        text = f"{num:.{digits}f}"
    return f"{text}{suffix}"


def _safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _bucket_recovery(score: Any) -> str:
    if score is None:
        return "unknown"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 67:
        return "green"
    if s >= 34:
        return "yellow"
    return "red"


def _asleep_ms(stage: dict) -> int | None:
    keys = (
        "total_slow_wave_sleep_time_milli",
        "total_rem_sleep_time_milli",
        "total_light_sleep_time_milli",
    )
    if not stage:
        return None
    parts = [stage.get(k) for k in keys]
    if any(p is None for p in parts):
        # Fall back to in-bed minus awake if present
        in_bed = stage.get("total_in_bed_time_milli")
        awake = stage.get("total_awake_time_milli")
        if in_bed is not None and awake is not None:
            return int(in_bed) - int(awake)
        return None
    return int(sum(parts))


def _ms_to_hhmm(ms: Any) -> str:
    if ms is None:
        return "—"
    total_minutes = int(ms) // 60000
    h, m = divmod(total_minutes, 60)
    return f"{h}h {m:02d}m"


def _duration(start: Any, end: Any) -> str:
    a, b = _parse_dt(start), _parse_dt(end)
    if not a or not b:
        return "—"
    minutes = int((b - a).total_seconds() // 60)
    h, m = divmod(minutes, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"
