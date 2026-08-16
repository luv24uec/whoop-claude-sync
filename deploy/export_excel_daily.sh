#!/bin/bash
# Pull latest Whoop sync from GitHub, refresh CSVs, rewrite ~/Whoop Excel files.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHOOP_LOCAL="${HOME}/ClaudeProjects/Whoop"
LOG_DIR="${ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/whoop-excel-export.log"
# Also mirror into ~/Library/Logs when permitted (LaunchAgent default).
LIB_LOG="${HOME}/Library/Logs/whoop-excel-export.log"
mkdir -p "${HOME}/Library/Logs" 2>/dev/null || true

exec >>"${LOG}" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

cd "${ROOT}"

# Stdlib-only exporter — pin a known macOS python path for launchd.
PYTHON="/usr/bin/python3"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"

# Refresh local archive from private GitHub repo (Actions is source of truth).
if command -v git >/dev/null 2>&1; then
  git pull --ff-only origin main || echo "WARN: git pull failed; using local files"
fi

mkdir -p "${WHOOP_LOCAL}/archive" "${WHOOP_LOCAL}/upload_pack" "${WHOOP_LOCAL}/csv"
if [[ -d "${ROOT}/output/archive" ]]; then
  rsync -a "${ROOT}/output/archive/" "${WHOOP_LOCAL}/archive/"
fi
for f in WHOOP_BRIEF.md WHOOP_LAST_7_DAYS.md WHOOP_LAST_30_DAYS.md WHOOP_WORKOUTS.md PROJECT_INSTRUCTIONS.md; do
  [[ -f "${ROOT}/output/${f}" ]] && cp "${ROOT}/output/${f}" "${WHOOP_LOCAL}/${f}"
done
if [[ -d "${ROOT}/output/upload_pack" ]]; then
  rsync -a --delete "${ROOT}/output/upload_pack/" "${WHOOP_LOCAL}/upload_pack/"
fi

# Rebuild analysis CSVs from archive (used by Excel export).
if [[ -x "${WHOOP_LOCAL}/analyze.sh" ]]; then
  bash "${WHOOP_LOCAL}/analyze.sh" || echo "WARN: analyze.sh failed"
fi

"${PYTHON}" "${ROOT}/deploy/export_excel.py"
echo "OK"
