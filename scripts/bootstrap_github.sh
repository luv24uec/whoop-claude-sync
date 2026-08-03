#!/bin/bash
# One-shot: create private repo, push, set Actions secrets.
# Requires: GH_TOKEN with scopes repo + workflow
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GH="/opt/homebrew/bin/gh"
if [[ ! -x "$GH" ]]; then GH="$(command -v gh)"; fi

# Prefer already-logged-in gh; optional GH_TOKEN for headless/CI.
if ! "$GH" auth status >/dev/null 2>&1; then
  if [[ -n "${GH_TOKEN:-}" ]]; then
    echo "$GH_TOKEN" | "$GH" auth login --with-token -h github.com
  else
    echo "Not logged into GitHub. Run: gh auth login"
    echo "Or set GH_TOKEN (classic PAT with repo + workflow scopes)."
    exit 1
  fi
fi

USER_LOGIN="$("$GH" api user -q .login)"
REPO_NAME="${REPO_NAME:-whoop-claude-sync}"
FULL="${USER_LOGIN}/${REPO_NAME}"

echo "GitHub user: $USER_LOGIN"
echo "Repo: $FULL (private)"

# Ensure local git repo
if [[ ! -d .git ]]; then
  git init
  git branch -M main
fi

mkdir -p state output
if [[ ! -f state/tokens.json && -f "$HOME/.config/whoop-claude-sync/tokens.json" ]]; then
  cp "$HOME/.config/whoop-claude-sync/tokens.json" state/tokens.json
  chmod 600 state/tokens.json
fi
if [[ ! -f output/WHOOP_BRIEF.md && -d "$HOME/ClaudeProjects/Whoop" ]]; then
  rsync -a "$HOME/ClaudeProjects/Whoop/" output/
fi

# Create private repo if missing
if ! "$GH" repo view "$FULL" >/dev/null 2>&1; then
  "$GH" repo create "$REPO_NAME" --private --source=. --remote=origin --push
else
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/${FULL}.git"
  git add -A
  git status --short
  if ! git diff --cached --quiet || ! git diff --quiet; then
    git add -A
    git -c user.name="whoop-claude-sync" -c user.email="${USER_LOGIN}@users.noreply.github.com" \
      commit -m "chore: bootstrap whoop-claude-sync for cloud sync" || true
  fi
  # ensure at least one commit exists
  if ! git rev-parse HEAD >/dev/null 2>&1; then
    git add -A
    git -c user.name="whoop-claude-sync" -c user.email="${USER_LOGIN}@users.noreply.github.com" \
      commit -m "chore: bootstrap whoop-claude-sync for cloud sync"
  fi
  git push -u origin main
fi

# Secrets from local config (never printed)
python3 - <<'PY'
import tomllib, pathlib, subprocess, os, json
root = pathlib.Path(".").resolve()
cfg = tomllib.loads((root / "config.toml").read_text())
client_id = cfg["client_id"]
client_secret = cfg["client_secret"]
tokens_path = root / "state" / "tokens.json"
if not tokens_path.exists():
    tokens_path = pathlib.Path.home() / ".config/whoop-claude-sync/tokens.json"
tokens = tokens_path.read_text()

def set_secret(name, value):
    subprocess.run(
        ["gh", "secret", "set", name, "--body", value],
        check=True,
        env=os.environ,
    )

set_secret("WHOOP_CLIENT_ID", client_id)
set_secret("WHOOP_CLIENT_SECRET", client_secret)
set_secret("WHOOP_TOKENS_JSON", tokens)
print("Secrets set: WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET, WHOOP_TOKENS_JSON")
PY

# Kick a test run
"$GH" workflow run "Whoop daily sync" || "$GH" workflow run daily.yml || true
sleep 3
"$GH" run list --limit 3 || true

echo ""
echo "Done. Repo: https://github.com/${FULL}"
echo "Actions: https://github.com/${FULL}/actions"
