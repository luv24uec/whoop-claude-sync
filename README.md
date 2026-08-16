# whoop-claude-sync

Pull your Whoop recovery / sleep / strain / workouts and dump Claude-friendly markdown into a local folder you can upload to a Claude Project.

```
Whoop API  →  archive/*.jsonl  →  WHOOP_BRIEF.md (+ friends)  →  Claude Project
```

> Claude.ai Projects have **no official upload API**. This tool writes files locally and stages an `upload_pack/` for drag-and-drop. Automate the Whoop side; upload to Claude when you want a refresh.

## Setup

### 1. Whoop developer app

1. Create an app at [developer.whoop.com](https://developer.whoop.com)
2. Set redirect URI to: `https://localhost:8787/callback`  
   (Whoop rejects plain `http://localhost`)
3. Copy **Client ID** and **Client Secret**

### 2. Install

```bash
cd ~/whoop-claude-sync
# needs Python 3.11+ (this machine: Homebrew python3.14)
# Keep the project OFF ~/Desktop — macOS blocks LaunchAgents from Desktop.
/opt/homebrew/bin/python3.14 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Configure

```bash
cp config.example.toml config.toml
# edit client_id / client_secret / out_dir
```

Or use env vars (see `.env.example`):

```bash
export WHOOP_CLIENT_ID=...
export WHOOP_CLIENT_SECRET=...
```

Default output: `~/ClaudeProjects/Whoop`

### 4. Authorize + sync

```bash
whoop-claude-sync init
whoop-claude-sync auth
whoop-claude-sync sync
whoop-claude-sync pack
```

`auth` opens Whoop consent. After approve, the browser hits `https://localhost:8787/callback` (certificate warning is normal — nothing is listening). Copy the **full URL** from the address bar and paste it into the terminal.

## Commands

| Command | What it does |
|---------|----------------|
| `init` | Create output folder + starter Project instructions |
| `auth` | OAuth login; store tokens in `~/.config/whoop-claude-sync/tokens.json` |
| `sync` | Fetch Whoop → merge JSONL → render markdown |
| `sync --days 30` | Override lookback window |
| `pack` | Copy curated `.md` files into `out/upload_pack/` |
| `status` | Token + last sync summary |

## Output layout

```
~/ClaudeProjects/Whoop/
  WHOOP_BRIEF.md              # upload this
  WHOOP_LAST_7_DAYS.md
  WHOOP_LAST_30_DAYS.md
  WHOOP_WORKOUTS.md
  PROJECT_INSTRUCTIONS.md     # paste into Claude custom instructions
  upload_pack/                # from `pack`
  archive/
    recovery.jsonl
    sleep.jsonl
    cycles.jsonl
    workouts.jsonl
    sync_meta.json
```

### Into Claude

1. Create a Project on [claude.ai](https://claude.ai)
2. Paste `PROJECT_INSTRUCTIONS.md` into custom instructions
3. Upload the `WHOOP_*.md` files (or everything in `upload_pack/`)
4. Re-run `sync && pack` daily/weekly and replace the files

## Scheduled sync (cloud — Mac can sleep)

Local LaunchAgents need your Mac awake. Prefer **GitHub Actions** on a **private** repo:

| Job | When (IST) | What |
|-----|------------|------|
| **Daily (morning)** | ~05:00 target (cron drifts) | Sync last 14 days → refresh brief + 7-day files |
| **Daily (evening)** | 21:30 | Same daily sync (evening refresh) |
| **Weekly** | Sundays ~06:00 target | Sync last 90 days → refresh trends + `upload_pack/` |

### One-time cloud setup (fast path — ~2 minutes)

Browser device-login automation is flaky. Use a **Personal Access Token** instead:

1. Create a classic PAT: https://github.com/settings/tokens/new  
   Scopes: **`repo`** + **`workflow`**  
   (Keep the repo **private** — it will hold Whoop tokens + health summaries.)

2. In Terminal:

```bash
export GH_TOKEN='paste_your_pat_here'
cd ~/whoop-claude-sync
./scripts/bootstrap_github.sh
```

That script creates the private repo, pushes code + `output/`, sets Actions secrets, and triggers a test daily sync.

Workflows: `.github/workflows/daily.yml` (07:15 IST) and `weekly.yml` (Sun 08:00 IST).  
They rotate/persist refresh tokens into `state/tokens.json` on each run.

### Pulling files onto this Mac (optional)

```bash
cd ~/whoop-claude-sync && git pull
open output/upload_pack   # drag into Claude Project when you want
```

Or point Claude Code at this private repo — no Mac wake needed for sync itself.

### Local schedules (not recommended)

`schedule install` still exists for LaunchAgents, but macOS must be awake at run time. Cloud cron is the reliable path.

## Notes

- Requires an **active paid Whoop membership** for API data
- Refresh tokens are **single-use** — cloud workflows persist the new token into `state/tokens.json`
- First `sync` backfills **90 days**; daily runs use **14 days**

### Critical: one writer for Whoop OAuth

Whoop **invalidates** the old refresh token every time a new one is issued.

If this Mac runs `sync` and GitHub Actions later uses the previous token, Actions fails with `Token refresh failed (400)` and your Excel/brief data goes stale.

**Rule:** GitHub Actions is the only process that should call the Whoop API day-to-day.

| Do | Don't |
|----|--------|
| Let Actions sync → `git pull` → Excel export | Run local `whoop-claude-sync sync` casually |
| If you must sync locally, **commit + push `state/tokens.json` immediately** | Leave rotated tokens only in `~/.config/.../tokens.json` |

Local noon Excel job (`deploy/export_excel_daily.sh`) only pulls from GitHub and rewrites `~/Whoop` — it does **not** talk to Whoop.

## Privacy

- Keep the GitHub repo **private**
- Do not commit `config.toml` or `.env`
- `state/tokens.json` is committed only so Actions can rotate tokens without your Mac — treat the repo as sensitive
