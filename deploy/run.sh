#!/bin/bash
# Reliable launcher for launchd / cron (sets PYTHONPATH + config).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/src"
cd "${ROOT}"

exec "${ROOT}/.venv/bin/python" -m whoop_claude_sync.cli "$@" --config "${ROOT}/config.toml"
