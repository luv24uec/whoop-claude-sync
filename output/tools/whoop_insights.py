#!/usr/bin/env python3
"""
whoop_insights.py — turn the local WHOOP database into an analysis digest.

Writes two files into the data directory:
  digest.md    a compact human-readable brief
  digest.json  the same numbers, structured, for programmatic use

Zero third-party dependencies. All statistics are implemented directly so this
never breaks on a numpy/pandas upgrade.

  python3 whoop_insights.py            # write both digest files
  python3 whoop_insights.py --print    # also print the brief to stdout
  python3 whoop_insights.py --weekly   # include the deeper weekly sections
"""

import argparse
import json
import math
import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

DATA_DIR = os.path.expanduser(os.environ.get("WHOOP_DATA_DIR", "~/WhoopData"))
DB_PATH = os.path.join(DATA_DIR, "whoop.db")
DIGEST_MD = os.path.join(DATA_DIR, "digest.md")
DIGEST_JSON = os.path.join(DATA_DIR, "digest.json")

KJ_PER_KCAL = 4.184
TARGET_BODY_FAT_PCT = float(os.environ.get("WHOOP_TARGET_BF", "10"))


# --------------------------------------------------------------------------
# Statistics (stdlib only)
# --------------------------------------------------------------------------

def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def stdev(xs):
    xs = [x for x in xs if x is not None]
    return statistics.pstdev(xs) if len(xs) > 1 else None


def zscore(value, baseline, spread):
    if value is None or baseline is None or not spread:
        return None
    return (value - baseline) / spread


def pearson(xs, ys):
    """Correlation over paired, non-null observations. Returns (r, n)."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 6:
        return None, n
    xs2 = [p[0] for p in pairs]
    ys2 = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs2), statistics.fmean(ys2)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs2))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys2))
    if dx == 0 or dy == 0:
        return None, n
    return num / (dx * dy), n


def significant(r, n):
    """Rough two-sided significance screen at p<0.05 via the t approximation."""
    if r is None or n < 6 or abs(r) >= 1:
        return False
    t = abs(r) * math.sqrt((n - 2) / (1 - r * r))
    # critical t at p=.05 falls quickly toward ~2.0 as n grows
    critical = {6: 2.78, 7: 2.57, 8: 2.45, 9: 2.36, 10: 2.31,
                12: 2.23, 15: 2.16, 20: 2.10, 30: 2.05}
    key = max(k for k in critical if k <= n) if n >= 6 else 6
    return t > (critical[key] if n < 30 else 2.0)


def linear_trend(days, values):
    """Least-squares slope per day over (day_index, value) pairs."""
    pairs = [(d, v) for d, v in zip(days, values) if v is not None]
    if len(pairs) < 4:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in pairs) / denom


def fmt(value, digits=1, suffix=""):
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def hhmm(milli):
    if milli is None:
        return "—"
    total = int(milli // 60000)
    return f"{total // 60}h {total % 60:02d}m"


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def load(conn, days_back=180):
    """Build one row per calendar day, joining every source."""
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    daily = defaultdict(lambda: {})

    for r in conn.execute(
        "SELECT day, strain, kilojoule, average_heart_rate, max_heart_rate "
        "FROM cycles WHERE day >= ? AND score_state='SCORED'", (cutoff,)
    ):
        d = daily[r["day"]]
        d["strain"] = r["strain"]
        d["kilojoule"] = r["kilojoule"]
        d["day_avg_hr"] = r["average_heart_rate"]

    for r in conn.execute(
        "SELECT day, recovery_score, resting_heart_rate, hrv_rmssd_milli, "
        "spo2_percentage, skin_temp_celsius FROM recovery "
        "WHERE day >= ? AND score_state='SCORED'", (cutoff,)
    ):
        d = daily[r["day"]]
        d["recovery"] = r["recovery_score"]
        d["rhr"] = r["resting_heart_rate"]
        d["hrv"] = r["hrv_rmssd_milli"]
        d["spo2"] = r["spo2_percentage"]
        d["skin_temp"] = r["skin_temp_celsius"]

    # Main sleep only (naps aggregated separately).
    for r in conn.execute(
        "SELECT day, nap, total_in_bed_milli, total_awake_milli, total_rem_milli, "
        "total_sws_milli, total_light_milli, disturbance_count, respiratory_rate, "
        "sleep_performance_pct, sleep_consistency_pct, sleep_efficiency_pct, "
        "need_baseline_milli, need_from_debt_milli, need_from_strain_milli "
        "FROM sleep WHERE day >= ? AND score_state='SCORED'", (cutoff,)
    ):
        d = daily[r["day"]]
        if r["nap"]:
            d["nap_milli"] = (d.get("nap_milli") or 0) + (r["total_in_bed_milli"] or 0)
            continue
        asleep = (r["total_in_bed_milli"] or 0) - (r["total_awake_milli"] or 0)
        d["sleep_milli"] = asleep
        d["sleep_hours"] = asleep / 3_600_000 if asleep else None
        d["rem_milli"] = r["total_rem_milli"]
        d["sws_milli"] = r["total_sws_milli"]
        d["disturbances"] = r["disturbance_count"]
        # Raw disturbance count rises simply because you slept longer, which
        # correlates positively with recovery and inverts the sign. Rate is the
        # honest measure of how broken the sleep actually was.
        d["disturbances_per_hour"] = (
            r["disturbance_count"] / (asleep / 3_600_000)
            if r["disturbance_count"] is not None and asleep else None)
        d["respiratory_rate"] = r["respiratory_rate"]
        d["sleep_performance"] = r["sleep_performance_pct"]
        d["sleep_consistency"] = r["sleep_consistency_pct"]
        d["sleep_efficiency"] = r["sleep_efficiency_pct"]
        need = (r["need_baseline_milli"] or 0) + (r["need_from_debt_milli"] or 0) \
            + (r["need_from_strain_milli"] or 0)
        d["sleep_need_milli"] = need or None
        d["sleep_debt_milli"] = r["need_from_debt_milli"]
        d["sleep_shortfall_milli"] = (need - asleep) if need and asleep else None

    for r in conn.execute(
        "SELECT day, COUNT(*) n, SUM(kilojoule) kj, MAX(strain) top_strain, "
        "GROUP_CONCAT(sport_name) sports FROM workouts "
        "WHERE day >= ? AND score_state='SCORED' GROUP BY day", (cutoff,)
    ):
        d = daily[r["day"]]
        d["workout_count"] = r["n"]
        d["workout_kj"] = r["kj"]
        d["top_workout_strain"] = r["top_strain"]
        d["sports"] = r["sports"]

    for r in conn.execute(
        "SELECT day, weight_kilogram FROM body_measurement WHERE day >= ?", (cutoff,)
    ):
        if r["weight_kilogram"]:
            daily[r["day"]]["weight_kg"] = r["weight_kilogram"]

    # Manual entries win over the WHOOP snapshot — they're the scale reading.
    for r in conn.execute(
        "SELECT day, metric, value FROM manual_log WHERE day >= ?", (cutoff,)
    ):
        key = {"weight": "weight_kg", "weight_kg": "weight_kg",
               "body_fat": "body_fat_pct", "body_fat_pct": "body_fat_pct",
               "waist": "waist_cm", "waist_cm": "waist_cm",
               "calories": "intake_kcal", "intake_kcal": "intake_kcal",
               "protein": "protein_g", "protein_g": "protein_g",
               "steps": "steps"}.get(r["metric"], r["metric"])
        daily[r["day"]][key] = r["value"]

    for r in conn.execute(
        "SELECT day, question, answer FROM journal WHERE day >= ?", (cutoff,)
    ):
        daily[r["day"]].setdefault("journal", {})[r["question"]] = r["answer"]

    rows = []
    for day in sorted(daily):
        row = daily[day]
        row["day"] = day
        rows.append(row)
    return rows


def series(rows, key):
    return [r.get(key) for r in rows]


def window(rows, n):
    return rows[-n:] if len(rows) >= 1 else []


# --------------------------------------------------------------------------
# Journal flag extraction
# --------------------------------------------------------------------------

JOURNAL_FLAGS = {
    "alcohol": ["alcohol", "drink"],
    "late_caffeine": ["caffeine"],
    "late_meal": ["late meal", "eat late", "ate late", "close to bedtime"],
    "screen": ["screen", "blue light", "device"],
    "stress": ["stress", "anxious"],
    "sick": ["sick", "ill", "symptom"],
    "travel": ["travel", "time zone", "jet"],
    "strength_train": ["strength", "lift", "resistance"],
    "fasting": ["fast", "intermittent"],
    "sauna": ["sauna", "heat"],
    "cold": ["cold", "ice bath", "plunge"],
    "read": ["read"],
    "meditate": ["meditat", "breathwork", "breath"],
}


def flag_value(journal, flag):
    """Return 1/0 if the day's journal answers a question matching this flag."""
    if not journal:
        return None
    keywords = JOURNAL_FLAGS[flag]
    for question, answer in journal.items():
        q = (question or "").lower()
        if any(k in q for k in keywords):
            a = (answer or "").strip().lower()
            if a in ("true", "yes", "1"):
                return 1.0
            if a in ("false", "no", "0"):
                return 0.0
    return None


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def analyse(rows):
    out = {"generated_at": datetime.now().astimezone().isoformat(),
           "days_of_data": len(rows)}
    if not rows:
        return out

    latest = rows[-1]
    out["latest_day"] = latest["day"]

    w30, w7, w14, w28, w90 = (window(rows, n) for n in (30, 7, 14, 28, 90))

    # ---- baselines and today's deviation --------------------------------
    baselines = {}
    for metric in ("recovery", "hrv", "rhr", "sleep_hours", "strain",
                   "respiratory_rate", "sleep_performance", "skin_temp"):
        vals30 = series(w30, metric)
        base = mean(vals30)
        spread = stdev(vals30)
        baselines[metric] = {
            "today": latest.get(metric),
            "mean_7d": mean(series(w7, metric)),
            "mean_30d": base,
            "sd_30d": spread,
            "z_today": zscore(latest.get(metric), base, spread),
        }
    out["baselines"] = baselines

    # ---- sleep ----------------------------------------------------------
    shortfalls = [r.get("sleep_shortfall_milli") for r in w7]
    out["sleep"] = {
        "last_night_hours": latest.get("sleep_hours"),
        "last_night_need_hours": (latest.get("sleep_need_milli") or 0) / 3_600_000 or None,
        "shortfall_last_night_min": (latest.get("sleep_shortfall_milli") or 0) / 60000
                                     if latest.get("sleep_shortfall_milli") else None,
        "cumulative_shortfall_7d_hours": sum(s for s in shortfalls if s and s > 0) / 3_600_000
                                          if any(s for s in shortfalls if s) else 0.0,
        "avg_hours_7d": mean(series(w7, "sleep_hours")),
        "avg_hours_30d": mean(series(w30, "sleep_hours")),
        "consistency_30d": mean(series(w30, "sleep_consistency")),
        "efficiency_30d": mean(series(w30, "sleep_efficiency")),
        "sd_hours_30d": stdev(series(w30, "sleep_hours")),
    }

    # ---- training load ---------------------------------------------------
    acute = mean(series(w7, "strain"))
    chronic = mean(series(w28, "strain"))
    out["load"] = {
        "strain_7d": acute,
        "strain_28d": chronic,
        "acute_chronic_ratio": (acute / chronic) if acute and chronic else None,
        "workouts_7d": sum(r.get("workout_count") or 0 for r in w7),
        "workouts_28d": sum(r.get("workout_count") or 0 for r in w28),
        "monotony_7d": (acute / stdev(series(w7, "strain")))
                        if acute and stdev(series(w7, "strain")) else None,
    }

    # ---- energy ----------------------------------------------------------
    def kcal(row):
        kj = row.get("kilojoule")
        return kj / KJ_PER_KCAL if kj else None

    burn7 = [kcal(r) for r in w7]
    burn28 = [kcal(r) for r in w28]
    out["energy"] = {
        "burn_yesterday_kcal": kcal(latest),
        "burn_avg_7d_kcal": mean(burn7),
        "burn_avg_28d_kcal": mean(burn28),
        "intake_avg_7d_kcal": mean(series(w7, "intake_kcal")),
        "protein_avg_7d_g": mean(series(w7, "protein_g")),
    }
    if out["energy"]["intake_avg_7d_kcal"] and out["energy"]["burn_avg_7d_kcal"]:
        deficit = out["energy"]["burn_avg_7d_kcal"] - out["energy"]["intake_avg_7d_kcal"]
        out["energy"]["deficit_avg_7d_kcal"] = deficit
        out["energy"]["implied_fat_loss_kg_per_week"] = deficit * 7 / 7700

    # ---- body composition ------------------------------------------------
    weights = [(i, r.get("weight_kg")) for i, r in enumerate(rows)]
    idx28 = [i for i, _ in weights][-28:]
    body = {
        "latest_weight_kg": next((r.get("weight_kg") for r in reversed(rows)
                                  if r.get("weight_kg")), None),
        "latest_body_fat_pct": next((r.get("body_fat_pct") for r in reversed(rows)
                                     if r.get("body_fat_pct")), None),
        "latest_waist_cm": next((r.get("waist_cm") for r in reversed(rows)
                                 if r.get("waist_cm")), None),
    }
    slope = linear_trend(idx28, [rows[i].get("weight_kg") for i in idx28]) if idx28 else None
    if slope is not None:
        body["weight_trend_kg_per_week"] = slope * 7
        if body["latest_weight_kg"]:
            body["weight_trend_pct_bw_per_week"] = slope * 7 / body["latest_weight_kg"] * 100

    bf = body["latest_body_fat_pct"]
    wt = body["latest_weight_kg"]
    if bf and wt:
        lean = wt * (1 - bf / 100)
        target_weight = lean / (1 - TARGET_BODY_FAT_PCT / 100)
        body["lean_mass_kg"] = lean
        body["fat_mass_kg"] = wt - lean
        body["target_body_fat_pct"] = TARGET_BODY_FAT_PCT
        body["target_weight_kg"] = target_weight
        body["kg_to_lose"] = wt - target_weight
        rate = body.get("weight_trend_kg_per_week")
        if rate and rate < -0.05 and wt > target_weight:
            weeks = (wt - target_weight) / abs(rate)
            body["weeks_to_target"] = weeks
            body["projected_target_date"] = (
                date.today() + timedelta(weeks=weeks)).isoformat()
    out["body"] = body

    # ---- correlations ----------------------------------------------------
    # Recovery is scored in the morning, so it reflects the night that just
    # ended and the strain of the day before. Lags are set accordingly.
    corr = {}

    def add(name, xs, ys, note):
        r, n = pearson(xs, ys)
        if r is not None:
            corr[name] = {"r": r, "n": n, "significant": significant(r, n), "note": note}

    rec = series(w90, "recovery")
    hrv = series(w90, "hrv")
    rhr = series(w90, "rhr")

    add("sleep_hours_to_recovery", series(w90, "sleep_hours"), rec,
        "More sleep the night before → higher recovery that morning")
    add("sleep_consistency_to_recovery", series(w90, "sleep_consistency"), rec,
        "Going to bed at consistent times → higher recovery")
    add("prior_day_strain_to_recovery", series(w90, "strain")[:-1], rec[1:],
        "Yesterday's strain → this morning's recovery")
    add("sws_to_recovery", series(w90, "sws_milli"), rec,
        "Deep (slow-wave) sleep → recovery")
    add("rem_to_recovery", series(w90, "rem_milli"), rec,
        "REM sleep → recovery")
    add("disturbance_rate_to_recovery", series(w90, "disturbances_per_hour"), rec,
        "Sleep disturbances per hour → recovery")
    add("efficiency_to_recovery", series(w90, "sleep_efficiency"), rec,
        "Sleep efficiency → recovery")
    add("respiratory_rate_to_recovery", series(w90, "respiratory_rate"), rec,
        "Respiratory rate → recovery")
    add("recovery_to_next_day_strain", rec[:-1], series(w90, "strain")[1:],
        "Do you actually train harder on high-recovery days?")

    for flag in JOURNAL_FLAGS:
        flags = [flag_value(r.get("journal"), flag) for r in w90]
        if sum(1 for f in flags if f is not None) < 8:
            continue
        add(f"{flag}_to_recovery", flags, rec, f"{flag.replace('_', ' ')} → recovery")
        add(f"{flag}_to_rhr", flags, rhr, f"{flag.replace('_', ' ')} → resting heart rate")
        add(f"{flag}_to_hrv", flags, hrv, f"{flag.replace('_', ' ')} → HRV")

    out["correlations"] = dict(
        sorted(corr.items(), key=lambda kv: -abs(kv[1]["r"]))
    )

    # ---- behavioural comparisons (effect sizes, not just r) --------------
    effects = {}
    for flag in JOURNAL_FLAGS:
        on, off = [], []
        for i, r in enumerate(w90):
            v = flag_value(r.get("journal"), flag)
            if v is None or r.get("recovery") is None:
                continue
            (on if v else off).append(r["recovery"])
        if len(on) >= 4 and len(off) >= 4:
            effects[flag] = {
                "recovery_on": mean(on), "n_on": len(on),
                "recovery_off": mean(off), "n_off": len(off),
                "delta": mean(on) - mean(off),
            }
    out["behaviour_effects"] = dict(
        sorted(effects.items(), key=lambda kv: kv[1]["delta"])
    )

    # ---- flags -----------------------------------------------------------
    alerts = []
    hrv_recent = [r.get("hrv") for r in window(rows, 3)]
    hrv_base = baselines["hrv"]["mean_30d"]
    hrv_sd = baselines["hrv"]["sd_30d"]
    if hrv_base and hrv_sd and all(v is not None and v < hrv_base - hrv_sd for v in hrv_recent) \
            and len(hrv_recent) == 3:
        alerts.append("HRV has been more than 1 SD below your 30-day baseline for 3 straight "
                      "days — that is a systemic stress signal, not a training one. "
                      "Check sleep, alcohol, illness, and calorie deficit depth.")

    rhr_z = baselines["rhr"]["z_today"]
    if rhr_z is not None and rhr_z > 1.5:
        alerts.append(f"Resting heart rate is {fmt(rhr_z, 1)} SD above baseline. "
                      "Common causes: under-recovery, illness onset, alcohol, or a "
                      "deficit that has run too deep for too long.")

    debt = out["sleep"]["cumulative_shortfall_7d_hours"]
    if debt and debt > 3:
        alerts.append(f"You are {fmt(debt, 1)}h short of your sleep need across the last 7 days. "
                      "Sleep restriction reliably raises ghrelin and lowers leptin — it makes a "
                      "cut harder to adhere to and shifts weight loss toward lean mass.")

    acr = out["load"]["acute_chronic_ratio"]
    if acr and acr > 1.4:
        alerts.append(f"Acute:chronic strain ratio is {fmt(acr, 2)} — you have ramped load fast. "
                      "This is the classic injury/overreaching window.")
    elif acr and acr < 0.75 and out["load"]["workouts_28d"]:
        alerts.append(f"Training load has dropped off ({fmt(acr, 2)} acute:chronic). "
                      "During a cut, maintaining resistance-training stimulus is what protects "
                      "lean mass.")

    rate = body.get("weight_trend_pct_bw_per_week")
    if rate is not None:
        if rate < -1.0:
            alerts.append(f"Weight is falling {fmt(abs(rate), 2)}% of body weight per week. "
                          "Above roughly 1%/week, an increasing share of the loss tends to come "
                          "from lean tissue.")
        elif -0.2 < rate < 0.2 and out["days_of_data"] > 21:
            alerts.append("Weight has been essentially flat over 4 weeks. If the goal is fat loss, "
                          "either intake or expenditure needs to move.")

    if baselines["recovery"]["mean_7d"] and baselines["recovery"]["mean_7d"] < 40:
        alerts.append(f"7-day average recovery is {fmt(baselines['recovery']['mean_7d'], 0)}%. "
                      "Sustained sub-40 recovery usually means the deficit, training load, and "
                      "sleep are not compatible with each other right now.")

    out["alerts"] = alerts
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def arrow(z, higher_is_better=True):
    if z is None:
        return ""
    if abs(z) < 0.5:
        return " (typical)"
    good = (z > 0) == higher_is_better
    direction = "above" if z > 0 else "below"
    marker = "✓" if good else "▲"
    return f" ({fmt(abs(z), 1)} SD {direction} baseline {marker})"


def render(a, weekly=False):
    if not a.get("latest_day"):
        return "# WHOOP digest\n\nNo scored data in the local database yet.\n"

    b = a["baselines"]
    s = a["sleep"]
    L = a["load"]
    e = a["energy"]
    body = a["body"]

    lines = [
        f"# WHOOP digest — {a['latest_day']}",
        f"_{a['days_of_data']} days of data · generated {a['generated_at'][:16].replace('T', ' ')}_",
        "",
        "## This morning",
        "",
        f"- **Recovery** {fmt(b['recovery']['today'], 0, '%')}"
        f"{arrow(b['recovery']['z_today'], True)} · 7-day avg {fmt(b['recovery']['mean_7d'], 0, '%')}",
        f"- **HRV** {fmt(b['hrv']['today'], 0, ' ms')}"
        f"{arrow(b['hrv']['z_today'], True)} · baseline {fmt(b['hrv']['mean_30d'], 0, ' ms')}",
        f"- **Resting HR** {fmt(b['rhr']['today'], 0, ' bpm')}"
        f"{arrow(b['rhr']['z_today'], False)} · baseline {fmt(b['rhr']['mean_30d'], 0, ' bpm')}",
        f"- **Sleep** {fmt(s['last_night_hours'], 1, 'h')} against a need of "
        f"{fmt(s['last_night_need_hours'], 1, 'h')} · 7-day avg {fmt(s['avg_hours_7d'], 1, 'h')}",
        f"- **Sleep debt (7d)** {fmt(s['cumulative_shortfall_7d_hours'], 1, 'h')}",
        f"- **Yesterday's strain** {fmt(b['strain']['today'], 1)} · "
        f"burn {fmt(e['burn_yesterday_kcal'], 0, ' kcal')}",
        "",
        "## Training load",
        "",
        f"- 7-day strain {fmt(L['strain_7d'], 1)} vs 28-day {fmt(L['strain_28d'], 1)} "
        f"(acute:chronic {fmt(L['acute_chronic_ratio'], 2)})",
        f"- Workouts: {L['workouts_7d']} in 7 days, {L['workouts_28d']} in 28",
        f"- Average daily burn: {fmt(e['burn_avg_7d_kcal'], 0, ' kcal')} (7d) · "
        f"{fmt(e['burn_avg_28d_kcal'], 0, ' kcal')} (28d)",
    ]

    if e.get("deficit_avg_7d_kcal"):
        lines.append(
            f"- Logged intake {fmt(e['intake_avg_7d_kcal'], 0, ' kcal')} → deficit "
            f"{fmt(e['deficit_avg_7d_kcal'], 0, ' kcal/day')} "
            f"(≈{fmt(e.get('implied_fat_loss_kg_per_week'), 2, ' kg/week')})")

    lines += ["", "## Body composition", ""]
    if body.get("latest_weight_kg"):
        lines.append(f"- Weight {fmt(body['latest_weight_kg'], 1, ' kg')}"
                     + (f" · trend {fmt(body.get('weight_trend_kg_per_week'), 2, ' kg/week')}"
                        if body.get("weight_trend_kg_per_week") is not None else ""))
    if body.get("latest_body_fat_pct"):
        lines.append(f"- Body fat {fmt(body['latest_body_fat_pct'], 1, '%')} · "
                     f"lean mass {fmt(body.get('lean_mass_kg'), 1, ' kg')} · "
                     f"fat mass {fmt(body.get('fat_mass_kg'), 1, ' kg')}")
        lines.append(f"- To reach {fmt(body.get('target_body_fat_pct'), 0, '%')} at current lean "
                     f"mass: {fmt(body.get('target_weight_kg'), 1, ' kg')} "
                     f"({fmt(body.get('kg_to_lose'), 1, ' kg')} to go)")
        if body.get("projected_target_date"):
            lines.append(f"- At the current rate that lands around "
                         f"**{body['projected_target_date']}** "
                         f"({fmt(body.get('weeks_to_target'), 0)} weeks)")
    if not body.get("latest_weight_kg") and not body.get("latest_body_fat_pct"):
        lines.append("- No weight or body-fat readings logged yet. "
                     "Add them with `whoop_log.py weight 78.4` / `whoop_log.py body_fat 18.2`.")

    if a["alerts"]:
        lines += ["", "## Worth your attention", ""]
        lines += [f"- {x}" for x in a["alerts"]]

    if weekly:
        effects = a.get("behaviour_effects") or {}
        if effects:
            lines += ["", "## What your own data says about your habits", "",
                      "| Behaviour | Recovery when yes | when no | Difference |",
                      "|---|---|---|---|"]
            for name, v in effects.items():
                lines.append(
                    f"| {name.replace('_', ' ')} | {fmt(v['recovery_on'], 0, '%')} "
                    f"(n={v['n_on']}) | {fmt(v['recovery_off'], 0, '%')} (n={v['n_off']}) | "
                    f"{v['delta']:+.0f} pts |")

        corr = a.get("correlations") or {}
        strong = {k: v for k, v in corr.items() if v["significant"]}
        if strong:
            lines += ["", "## Statistically meaningful relationships", "",
                      "| Relationship | r | n |", "|---|---|---|"]
            for name, v in list(strong.items())[:12]:
                lines.append(f"| {v['note']} | {v['r']:+.2f} | {v['n']} |")
            lines += ["", "_Correlation is not causation, and n is small for personal data. "
                      "Treat these as hypotheses to test, not conclusions._"]

        lines += ["", "## 30-day reference", "",
                  f"- Recovery {fmt(b['recovery']['mean_30d'], 0, '%')} "
                  f"(SD {fmt(b['recovery']['sd_30d'], 0)})",
                  f"- HRV {fmt(b['hrv']['mean_30d'], 0, ' ms')} "
                  f"(SD {fmt(b['hrv']['sd_30d'], 0)})",
                  f"- RHR {fmt(b['rhr']['mean_30d'], 0, ' bpm')} "
                  f"(SD {fmt(b['rhr']['sd_30d'], 0)})",
                  f"- Sleep {fmt(s['avg_hours_30d'], 1, 'h')} "
                  f"(SD {fmt(s['sd_hours_30d'], 1, 'h')}) · "
                  f"consistency {fmt(s['consistency_30d'], 0, '%')} · "
                  f"efficiency {fmt(s['efficiency_30d'], 0, '%')}"]

    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="do_print", action="store_true")
    ap.add_argument("--weekly", action="store_true", help="include deeper weekly sections")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"No database at {DB_PATH}. Run whoop_sync.py first.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = load(conn)
    result = analyse(rows)
    markdown = render(result, weekly=args.weekly)

    with open(DIGEST_MD, "w") as fh:
        fh.write(markdown)
    with open(DIGEST_JSON, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    print(f"Wrote {DIGEST_MD} and {DIGEST_JSON}")
    if args.do_print:
        print()
        print(markdown)


if __name__ == "__main__":
    main()
