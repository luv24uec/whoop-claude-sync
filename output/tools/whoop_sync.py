#!/usr/bin/env python3
"""
whoop_sync.py — pull WHOOP data into a local SQLite database + CSV mirrors.

Zero third-party dependencies: standard library only, so it runs on any macOS
Python 3.9+ without pip, virtualenvs, or breakage when Apple updates the OS.

Usage
-----
  python3 whoop_sync.py --auth                 # one-time browser authorization
  python3 whoop_sync.py                        # incremental sync (what cron runs)
  python3 whoop_sync.py --backfill 2023-01-01  # pull full history from a date
  python3 whoop_sync.py --status               # show what's stored locally

Design notes
------------
* Every record is stored twice: flattened into typed columns for querying, and
  as raw JSON. If WHOOP changes its schema, no data is ever lost — the flatteners
  can be re-run against the raw blobs.
* Records are upserted by primary key, and each incremental sync re-reads a
  trailing overlap window, because WHOOP re-scores recent records
  (score_state moves PENDING_SCORE -> SCORED hours later).
* Refresh tokens rotate on every use. The new token is persisted BEFORE the
  access token is used for anything, so a crash mid-sync can never orphan auth.
"""

import argparse
import csv
import http.server
import json
import os
import queue
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"

SCOPES = [
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
    "offline",  # required to receive a refresh token
]

DEFAULT_REDIRECT = "http://localhost:8788/callback"
KEYCHAIN_SERVICE = "whoop-pipeline"

DATA_DIR = os.path.expanduser(os.environ.get("WHOOP_DATA_DIR", "~/WhoopData"))
DB_PATH = os.path.join(DATA_DIR, "whoop.db")
CSV_DIR = os.path.join(DATA_DIR, "csv")
CRED_FALLBACK = os.path.join(DATA_DIR, ".credentials.json")
LOG_PATH = os.path.join(DATA_DIR, "sync.log")

# WHOOP allows 100 req/min. We stay well under it.
MIN_REQUEST_INTERVAL = 0.7
# Re-read this many days on every incremental sync to catch late re-scoring.
OVERLAP_DAYS = 7
PAGE_LIMIT = 25


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def die(msg, code=1):
    log(f"ERROR: {msg}")
    sys.exit(code)


def iso(dt):
    """UTC ISO-8601 with milliseconds, the format WHOOP expects."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_ts(value):
    """WHOOP timestamps -> aware datetime, tolerant of format drift."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(value.replace("Z", "+0000"), fmt)
            except ValueError:
                continue
    return None


def dig(obj, *path, default=None):
    """Safe nested lookup: dig(rec, 'score', 'stage_summary', 'total_rem_sleep_time_milli')."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def local_day(record, key="start"):
    """
    The calendar day a record belongs to, in the user's own timezone at the time
    — not UTC. A 11pm workout in Delhi must not be filed under the next day.
    """
    ts = parse_ts(record.get(key))
    if ts is None:
        return None
    offset = record.get("timezone_offset")  # e.g. "+05:30"
    if isinstance(offset, str) and len(offset) >= 5 and offset[0] in "+-":
        try:
            sign = 1 if offset[0] == "+" else -1
            hours = int(offset[1:3])
            minutes = int(offset[-2:])
            ts = ts.astimezone(timezone(sign * timedelta(hours=hours, minutes=minutes)))
        except ValueError:
            pass
    return ts.date().isoformat()


# --------------------------------------------------------------------------
# Credential storage (macOS Keychain, with an encrypted-at-rest-ish fallback)
# --------------------------------------------------------------------------

class Credentials:
    """Stores client_id, client_secret, refresh_token, redirect_uri."""

    def __init__(self):
        self.data = self._load()

    # -- backing stores ----------------------------------------------------
    @staticmethod
    def _keychain_available():
        return sys.platform == "darwin" and _which("security")

    def _load(self):
        if self._keychain_available():
            try:
                out = subprocess.run(
                    ["security", "find-generic-password",
                     "-s", KEYCHAIN_SERVICE, "-a", os.environ.get("USER", "whoop"), "-w"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                if out:
                    return json.loads(out)
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                pass
        if os.path.exists(CRED_FALLBACK):
            try:
                with open(CRED_FALLBACK) as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def save(self):
        blob = json.dumps(self.data)
        if self._keychain_available():
            try:
                subprocess.run(
                    ["security", "add-generic-password",
                     "-s", KEYCHAIN_SERVICE, "-a", os.environ.get("USER", "whoop"),
                     "-w", blob, "-U"],
                    capture_output=True, text=True, check=True,
                )
                return
            except subprocess.CalledProcessError as exc:
                log(f"Keychain write failed ({exc.stderr.strip()}), using file fallback.")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CRED_FALLBACK, "w") as fh:
            json.dump(self.data, fh)
        os.chmod(CRED_FALLBACK, 0o600)

    # -- accessors ---------------------------------------------------------
    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, **kwargs):
        self.data.update(kwargs)
        self.save()

    @property
    def configured(self):
        return bool(self.data.get("client_id") and self.data.get("client_secret"))


def _which(name):
    for path in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class WhoopClient:
    def __init__(self, creds):
        self.creds = creds
        self.access_token = None
        self.access_expiry = 0.0
        self._last_call = 0.0

    # -- token handling ----------------------------------------------------
    def _post_token(self, payload):
        body = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            TOKEN_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise RuntimeError(f"token request failed ({exc.code}): {detail}") from None

    def exchange_code(self, code, redirect_uri):
        tok = self._post_token({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.creds.get("client_id"),
            "client_secret": self.creds.get("client_secret"),
            "redirect_uri": redirect_uri,
        })
        self._store_tokens(tok)
        return tok

    def refresh(self):
        refresh_token = self.creds.get("refresh_token")
        if not refresh_token:
            die("No refresh token stored. Run:  python3 whoop_sync.py --auth")
        tok = self._post_token({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.creds.get("client_id"),
            "client_secret": self.creds.get("client_secret"),
            "scope": "offline",
        })
        self._store_tokens(tok)

    def _store_tokens(self, tok):
        # Persist the rotated refresh token FIRST. WHOOP invalidates the old one
        # the moment this call succeeds; losing it means re-authorizing by hand.
        if tok.get("refresh_token"):
            self.creds.set(refresh_token=tok["refresh_token"])
        self.access_token = tok.get("access_token")
        self.access_expiry = time.time() + int(tok.get("expires_in", 3600)) - 120

    def _ensure_token(self):
        if not self.access_token or time.time() >= self.access_expiry:
            self.refresh()

    # -- requests ----------------------------------------------------------
    def get(self, path, params=None, _retries=4):
        self._ensure_token()

        gap = time.time() - self._last_call
        if gap < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - gap)

        url = f"{API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        })

        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                self._last_call = time.time()
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            self._last_call = time.time()
            if exc.code == 401 and _retries > 0:
                self.access_token = None
                return self.get(path, params, _retries - 1)
            if exc.code == 429 and _retries > 0:
                wait = int(exc.headers.get("X-RateLimit-Reset") or 60)
                log(f"Rate limited; sleeping {wait}s")
                time.sleep(min(wait, 120))
                return self.get(path, params, _retries - 1)
            if exc.code >= 500 and _retries > 0:
                time.sleep(2 ** (4 - _retries))
                return self.get(path, params, _retries - 1)
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"GET {path} failed ({exc.code}): {detail}") from None
        except urllib.error.URLError as exc:
            if _retries > 0:
                time.sleep(2 ** (4 - _retries))
                return self.get(path, params, _retries - 1)
            raise RuntimeError(f"GET {path} network error: {exc.reason}") from None

    def paginate(self, path, start, end):
        """Yield every record in [start, end), following next_token."""
        token = None
        pages = 0
        while True:
            payload = self.get(path, {
                "start": iso(start),
                "end": iso(end),
                "limit": PAGE_LIMIT,
                "nextToken": token,
            })
            for record in payload.get("records") or []:
                yield record
            token = payload.get("next_token")
            pages += 1
            if not token or pages > 400:  # hard stop against a pathological loop
                break


# --------------------------------------------------------------------------
# OAuth authorization (one-time)
# --------------------------------------------------------------------------

_callback_queue = queue.Queue()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _callback_queue.put(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in params
        msg = ("WHOOP connected. You can close this tab and return to the terminal."
               if ok else
               f"Authorization failed: {params.get('error', ['unknown'])[0]}")
        self.wfile.write(f"""<!doctype html><html><head><meta charset="utf-8">
<title>WHOOP</title></head>
<body style="font-family:-apple-system,system-ui,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0;background:#0f1115;color:#e8eaed">
<div style="text-align:center"><div style="font-size:40px">{'&#10003;' if ok else '&#10007;'}</div>
<h2 style="font-weight:600">{msg}</h2></div></body></html>""".encode())

    def log_message(self, *args):  # silence the default stderr spam
        return


def authorize(creds):
    print("\n=== WHOOP one-time authorization ===\n")

    if not creds.configured:
        print("Create a free app at https://developer-dashboard.whoop.com")
        print("  • Scopes to tick: read:recovery, read:cycles, read:sleep,")
        print("                    read:workout, read:profile, read:body_measurement")
        print(f"  • Redirect URI:   {DEFAULT_REDIRECT}\n")
        client_id = input("Client ID: ").strip()
        client_secret = input("Client Secret: ").strip()
        redirect = input(f"Redirect URI [{DEFAULT_REDIRECT}]: ").strip() or DEFAULT_REDIRECT
        if not client_id or not client_secret:
            die("Client ID and Secret are both required.")
        creds.set(client_id=client_id, client_secret=client_secret, redirect_uri=redirect)

    redirect_uri = creds.get("redirect_uri", DEFAULT_REDIRECT)
    state = secrets.token_urlsafe(16)  # WHOOP requires >= 8 chars
    auth_link = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": creds.get("client_id"),
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
    })

    server = None
    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    if parsed_redirect.hostname in ("localhost", "127.0.0.1"):
        try:
            server = http.server.HTTPServer(
                ("127.0.0.1", parsed_redirect.port or 8788), _CallbackHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        except OSError as exc:
            log(f"Could not start local callback listener ({exc}); falling back to paste mode.")
            server = None

    print("Opening your browser to approve access...")
    print(f"If it doesn't open, paste this into your browser:\n\n{auth_link}\n")
    try:
        webbrowser.open(auth_link)
    except Exception:
        pass

    params = None
    if server:
        try:
            params = _callback_queue.get(timeout=300)
        except queue.Empty:
            log("Timed out waiting for the browser callback.")
        finally:
            server.shutdown()

    if not params:
        pasted = input("Paste the full URL you were redirected to: ").strip()
        params = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)

    if params.get("state", [None])[0] != state:
        die("State mismatch — possible CSRF, or you used a stale authorization link.")
    code = params.get("code", [None])[0]
    if not code:
        die(f"No authorization code returned: {params}")

    client = WhoopClient(creds)
    client.exchange_code(code, redirect_uri)
    profile = client.get("/v2/user/profile/basic")
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    print(f"\nConnected as {name or profile.get('email', 'your WHOOP account')}.")
    print("Now run:  python3 whoop_sync.py --backfill 2024-01-01\n")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY, day TEXT, start_ts TEXT, end_ts TEXT,
    timezone_offset TEXT, score_state TEXT,
    strain REAL, kilojoule REAL, average_heart_rate REAL, max_heart_rate REAL,
    raw TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycles_day ON cycles(day);

CREATE TABLE IF NOT EXISTS recovery (
    cycle_id INTEGER PRIMARY KEY, sleep_id TEXT, day TEXT, score_state TEXT,
    user_calibrating INTEGER, recovery_score REAL, resting_heart_rate REAL,
    hrv_rmssd_milli REAL, spo2_percentage REAL, skin_temp_celsius REAL,
    raw TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recovery_day ON recovery(day);

CREATE TABLE IF NOT EXISTS sleep (
    id TEXT PRIMARY KEY, day TEXT, start_ts TEXT, end_ts TEXT,
    timezone_offset TEXT, nap INTEGER, score_state TEXT,
    total_in_bed_milli REAL, total_awake_milli REAL, total_light_milli REAL,
    total_sws_milli REAL, total_rem_milli REAL, total_no_data_milli REAL,
    sleep_cycle_count INTEGER, disturbance_count INTEGER,
    need_baseline_milli REAL, need_from_debt_milli REAL,
    need_from_strain_milli REAL, need_from_nap_milli REAL,
    respiratory_rate REAL, sleep_performance_pct REAL,
    sleep_consistency_pct REAL, sleep_efficiency_pct REAL,
    raw TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sleep_day ON sleep(day);

CREATE TABLE IF NOT EXISTS workouts (
    id TEXT PRIMARY KEY, day TEXT, start_ts TEXT, end_ts TEXT,
    timezone_offset TEXT, sport_name TEXT, score_state TEXT,
    strain REAL, average_heart_rate REAL, max_heart_rate REAL, kilojoule REAL,
    percent_recorded REAL, distance_meter REAL, altitude_gain_meter REAL,
    zone_zero_milli REAL, zone_one_milli REAL, zone_two_milli REAL,
    zone_three_milli REAL, zone_four_milli REAL, zone_five_milli REAL,
    raw TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workouts_day ON workouts(day);

CREATE TABLE IF NOT EXISTS body_measurement (
    day TEXT PRIMARY KEY, height_meter REAL, weight_kilogram REAL,
    max_heart_rate REAL, raw TEXT
);

-- Journal answers come from the WHOOP CSV export, not the API.
CREATE TABLE IF NOT EXISTS journal (
    day TEXT, question TEXT, answer TEXT,
    PRIMARY KEY (day, question)
);

-- Anything you log by hand: scale weight, body-fat %, waist, calories.
CREATE TABLE IF NOT EXISTS manual_log (
    day TEXT, metric TEXT, value REAL, note TEXT,
    PRIMARY KEY (day, metric)
);

CREATE TABLE IF NOT EXISTS sync_state (
    collection TEXT PRIMARY KEY, last_synced TEXT, record_count INTEGER
);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, table, row):
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols)
    pk = {"cycles": "id", "recovery": "cycle_id", "sleep": "id",
          "workouts": "id", "body_measurement": "day"}[table]
    conn.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


# --------------------------------------------------------------------------
# Flatteners
# --------------------------------------------------------------------------

def flat_cycle(r):
    return {
        "id": r.get("id"),
        "day": local_day(r),
        "start_ts": r.get("start"), "end_ts": r.get("end"),
        "timezone_offset": r.get("timezone_offset"),
        "score_state": r.get("score_state"),
        "strain": dig(r, "score", "strain"),
        "kilojoule": dig(r, "score", "kilojoule"),
        "average_heart_rate": dig(r, "score", "average_heart_rate"),
        "max_heart_rate": dig(r, "score", "max_heart_rate"),
        "raw": json.dumps(r), "updated_at": r.get("updated_at"),
    }


def flat_recovery(r, day_lookup):
    cycle_id = r.get("cycle_id")
    return {
        "cycle_id": cycle_id,
        "sleep_id": r.get("sleep_id"),
        "day": day_lookup.get(cycle_id) or local_day(r, "created_at"),
        "score_state": r.get("score_state"),
        "user_calibrating": 1 if dig(r, "score", "user_calibrating") else 0,
        "recovery_score": dig(r, "score", "recovery_score"),
        "resting_heart_rate": dig(r, "score", "resting_heart_rate"),
        "hrv_rmssd_milli": dig(r, "score", "hrv_rmssd_milli"),
        "spo2_percentage": dig(r, "score", "spo2_percentage"),
        "skin_temp_celsius": dig(r, "score", "skin_temp_celsius"),
        "raw": json.dumps(r), "updated_at": r.get("updated_at"),
    }


def flat_sleep(r):
    ss = dig(r, "score", "stage_summary", default={}) or {}
    need = dig(r, "score", "sleep_needed", default={}) or {}
    return {
        "id": str(r.get("id")),
        "day": local_day(r, "end"),  # a sleep belongs to the day you wake up
        "start_ts": r.get("start"), "end_ts": r.get("end"),
        "timezone_offset": r.get("timezone_offset"),
        "nap": 1 if r.get("nap") else 0,
        "score_state": r.get("score_state"),
        "total_in_bed_milli": ss.get("total_in_bed_time_milli"),
        "total_awake_milli": ss.get("total_awake_time_milli"),
        "total_light_milli": ss.get("total_light_sleep_time_milli"),
        "total_sws_milli": ss.get("total_slow_wave_sleep_time_milli"),
        "total_rem_milli": ss.get("total_rem_sleep_time_milli"),
        "total_no_data_milli": ss.get("total_no_data_time_milli"),
        "sleep_cycle_count": ss.get("sleep_cycle_count"),
        "disturbance_count": ss.get("disturbance_count"),
        "need_baseline_milli": need.get("baseline_milli"),
        "need_from_debt_milli": need.get("need_from_sleep_debt_milli"),
        "need_from_strain_milli": need.get("need_from_recent_strain_milli"),
        "need_from_nap_milli": need.get("need_from_recent_nap_milli"),
        "respiratory_rate": dig(r, "score", "respiratory_rate"),
        "sleep_performance_pct": dig(r, "score", "sleep_performance_percentage"),
        "sleep_consistency_pct": dig(r, "score", "sleep_consistency_percentage"),
        "sleep_efficiency_pct": dig(r, "score", "sleep_efficiency_percentage"),
        "raw": json.dumps(r), "updated_at": r.get("updated_at"),
    }


def flat_workout(r):
    z = dig(r, "score", "zone_durations", default={}) or {}
    # v2 renamed the zone block; accept either shape.
    if not z:
        z = dig(r, "score", "zone_duration", default={}) or {}
    return {
        "id": str(r.get("id")),
        "day": local_day(r),
        "start_ts": r.get("start"), "end_ts": r.get("end"),
        "timezone_offset": r.get("timezone_offset"),
        "sport_name": r.get("sport_name") or r.get("sport_id"),
        "score_state": r.get("score_state"),
        "strain": dig(r, "score", "strain"),
        "average_heart_rate": dig(r, "score", "average_heart_rate"),
        "max_heart_rate": dig(r, "score", "max_heart_rate"),
        "kilojoule": dig(r, "score", "kilojoule"),
        "percent_recorded": dig(r, "score", "percent_recorded"),
        "distance_meter": dig(r, "score", "distance_meter"),
        "altitude_gain_meter": dig(r, "score", "altitude_gain_meter"),
        "zone_zero_milli": z.get("zone_zero_milli"),
        "zone_one_milli": z.get("zone_one_milli"),
        "zone_two_milli": z.get("zone_two_milli"),
        "zone_three_milli": z.get("zone_three_milli"),
        "zone_four_milli": z.get("zone_four_milli"),
        "zone_five_milli": z.get("zone_five_milli"),
        "raw": json.dumps(r), "updated_at": r.get("updated_at"),
    }


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def sync(conn, client, start, end):
    counts = {}

    log(f"Syncing {start.date()} -> {end.date()}")

    cycles = list(client.paginate("/v2/cycle", start, end))
    day_lookup = {}
    for rec in cycles:
        row = flat_cycle(rec)
        day_lookup[row["id"]] = row["day"]
        upsert(conn, "cycles", row)
    counts["cycles"] = len(cycles)

    # Fill in cycle->day for recoveries whose cycle predates this window.
    for row in conn.execute("SELECT id, day FROM cycles"):
        day_lookup.setdefault(row["id"], row["day"])

    recoveries = list(client.paginate("/v2/recovery", start, end))
    for rec in recoveries:
        upsert(conn, "recovery", flat_recovery(rec, day_lookup))
    counts["recovery"] = len(recoveries)

    sleeps = list(client.paginate("/v2/activity/sleep", start, end))
    for rec in sleeps:
        upsert(conn, "sleep", flat_sleep(rec))
    counts["sleep"] = len(sleeps)

    workouts = list(client.paginate("/v2/activity/workout", start, end))
    for rec in workouts:
        upsert(conn, "workouts", flat_workout(rec))
    counts["workouts"] = len(workouts)

    # Body measurement is a point-in-time snapshot; store one row per sync day
    # so a weight series builds up over time.
    try:
        body = client.get("/v2/user/measurement/body")
        upsert(conn, "body_measurement", {
            "day": datetime.now().date().isoformat(),
            "height_meter": body.get("height_meter"),
            "weight_kilogram": body.get("weight_kilogram"),
            "max_heart_rate": body.get("max_heart_rate"),
            "raw": json.dumps(body),
        })
        counts["body"] = 1
    except RuntimeError as exc:
        log(f"Body measurement unavailable: {exc}")

    now = datetime.now(timezone.utc).isoformat()
    for collection, n in counts.items():
        conn.execute(
            "INSERT INTO sync_state (collection, last_synced, record_count) VALUES (?,?,?) "
            "ON CONFLICT(collection) DO UPDATE SET last_synced=excluded.last_synced, "
            "record_count=COALESCE(sync_state.record_count,0)+excluded.record_count",
            (collection, now, n),
        )
    conn.commit()
    realign_days(conn)
    log("Fetched: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


def realign_days(conn):
    """
    Put every record on the day WHOOP itself would show it.

    A WHOOP cycle begins at sleep onset, so a cycle that starts at 23:54 on the
    31st is really the 1st. The recovery scored from that sleep is "the 1st's
    recovery". Filing by raw start timestamp splits a single physiological day
    across two calendar days and blanks out the most recent one.

    The fix: anchor everything to the morning you woke up.
      recovery.day  <- the wake day of its linked sleep
      cycles.day    <- the day of its linked recovery
    Workouts keep their own local day, which is already correct.
    """
    conn.execute("""
        UPDATE recovery SET day = (
            SELECT s.day FROM sleep s WHERE s.id = recovery.sleep_id AND s.day IS NOT NULL)
        WHERE sleep_id IN (SELECT id FROM sleep WHERE day IS NOT NULL)
    """)
    conn.execute("""
        UPDATE cycles SET day = (
            SELECT r.day FROM recovery r WHERE r.cycle_id = cycles.id AND r.day IS NOT NULL)
        WHERE id IN (SELECT cycle_id FROM recovery WHERE day IS NOT NULL)
    """)
    conn.commit()


def last_sync_time(conn):
    row = conn.execute("SELECT MIN(last_synced) AS t FROM sync_state").fetchone()
    if row and row["t"]:
        try:
            return datetime.fromisoformat(row["t"])
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------
# CSV mirrors — so anything (Claude, Excel, Sheets) can read without SQLite
# --------------------------------------------------------------------------

def export_csv(conn):
    os.makedirs(CSV_DIR, exist_ok=True)
    tables = ["cycles", "recovery", "sleep", "workouts",
              "body_measurement", "journal", "manual_log"]
    for table in tables:
        cursor = conn.execute(f"SELECT * FROM {table}")
        cols = [c[0] for c in cursor.description if c[0] != "raw"]
        path = os.path.join(CSV_DIR, f"{table}.csv")
        with open(path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            for row in cursor:
                writer.writerow([row[c] for c in cols])
    log(f"CSV mirrors written to {CSV_DIR}")


def show_status(conn):
    print(f"\nDatabase: {DB_PATH}")
    for table in ["cycles", "recovery", "sleep", "workouts",
                  "body_measurement", "journal", "manual_log"]:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        rng = conn.execute(
            f"SELECT MIN(day), MAX(day) FROM {table}").fetchone() if n else (None, None)
        span = f"  {rng[0]} → {rng[1]}" if n and rng[0] else ""
        print(f"  {table:<18} {n:>6} rows{span}")
    last = last_sync_time(conn)
    print(f"\nLast sync: {last.astimezone().strftime('%Y-%m-%d %H:%M') if last else 'never'}\n")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Sync WHOOP data to a local database.")
    ap.add_argument("--auth", action="store_true", help="run one-time authorization")
    ap.add_argument("--backfill", metavar="YYYY-MM-DD", help="pull full history from this date")
    ap.add_argument("--status", action="store_true", help="show local database summary")
    ap.add_argument("--no-csv", action="store_true", help="skip CSV mirror export")
    args = ap.parse_args()

    creds = Credentials()

    if args.auth:
        authorize(creds)
        return

    conn = connect()

    if args.status:
        show_status(conn)
        return

    if not creds.configured:
        die("Not configured yet. Run:  python3 whoop_sync.py --auth")

    client = WhoopClient(creds)
    end = datetime.now(timezone.utc) + timedelta(minutes=5)

    if args.backfill:
        try:
            start = datetime.strptime(args.backfill, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            die("--backfill needs YYYY-MM-DD")
        # Walk in 90-day windows: gentler on pagination and easier to resume.
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(days=90), end)
            sync(conn, client, cursor, window_end)
            cursor = window_end
    else:
        last = last_sync_time(conn)
        start = (last - timedelta(days=OVERLAP_DAYS)) if last else (end - timedelta(days=30))
        sync(conn, client, start, end)

    if not args.no_csv:
        export_csv(conn)

    show_status(conn)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as exc:
        die(str(exc))
