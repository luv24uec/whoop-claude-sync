#!/bin/bash
# Runs the analysis over whatever the Cursor sync has written into
# ClaudeProjects/Whoop/archive, and refreshes digest.md / digest.json.
#
#   bash analyze.sh              # daily digest
#   bash analyze.sh --weekly     # deeper weekly analysis
#
# Safe to run any time. It only reads the archive; it never modifies it.
#
# The working SQLite database lives outside this folder (in ~/.whoop-analysis)
# because network and virtualised mounts don't support the file locking SQLite
# needs. Only the finished digests get written back here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export WHOOP_DATA_DIR="${WHOOP_ANALYSIS_DIR:-$HOME/.whoop-analysis}"
mkdir -p "$WHOOP_DATA_DIR"

python3 "$HERE/tools/ingest_jsonl.py" "$HERE/archive" >/dev/null
python3 "$HERE/tools/whoop_insights.py" --print "$@"

# Publish the results back next to the raw data so Claude can read them.
cp "$WHOOP_DATA_DIR/digest.md"   "$HERE/DIGEST.md"   2>/dev/null || true
cp "$WHOOP_DATA_DIR/digest.json" "$HERE/DIGEST.json" 2>/dev/null || true
mkdir -p "$HERE/csv" && cp "$WHOOP_DATA_DIR"/csv/*.csv "$HERE/csv/" 2>/dev/null || true

echo
echo "Wrote $HERE/DIGEST.md and DIGEST.json"
