#!/bin/bash
# Digest Pipeline — run digest + podcast for a given config
#
# Usage:
#   ./run.sh /path/to/config.json
#   ./run.sh /path/to/config.json --dry-run

set -euo pipefail

CONFIG="${1:?Usage: run.sh /path/to/config.json [--dry-run]}"
EXTRA_ARGS="${@:2}"

# Resolve script directory (works even if called via symlink or cron)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Use venv python if available, otherwise system python3
if [ -f "/home/clawdbot/.openclaw/venv/bin/python3" ]; then
    PYTHON="/home/clawdbot/.openclaw/venv/bin/python3"
else
    PYTHON="python3"
fi

echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Starting digest pipeline: $CONFIG"

$PYTHON "$SCRIPT_DIR/scripts/digest.py" --config "$CONFIG" $EXTRA_ARGS 2>&1

$PYTHON "$SCRIPT_DIR/scripts/podcast.py" --config "$CONFIG" $EXTRA_ARGS 2>&1 || echo "Podcast generation failed (non-fatal)"

echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Pipeline complete"
