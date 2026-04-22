#!/bin/bash
# Digest Pipeline — run digest + podcast for a given config
#
# Usage:
#   ./run.sh /path/to/config.json
#   ./run.sh /path/to/config.json --dry-run

set -euo pipefail

CONFIG="${1:?Usage: run.sh /path/to/config.json [--dry-run]}"
EXTRA_ARGS="${@:2}"

echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Starting digest pipeline: $CONFIG"

# Prefer .venv CLI, fall back to system CLI, then module execution
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIGEST_EXIT=0
if [ -x "$SCRIPT_DIR/.venv/bin/digest-pipeline" ]; then
    "$SCRIPT_DIR/.venv/bin/digest-pipeline" "$CONFIG" $EXTRA_ARGS 2>&1 || DIGEST_EXIT=$?
elif command -v digest-pipeline &>/dev/null; then
    digest-pipeline "$CONFIG" $EXTRA_ARGS 2>&1 || DIGEST_EXIT=$?
else
    if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
        PYTHON="$SCRIPT_DIR/.venv/bin/python3"
    else
        PYTHON="python3"
    fi

    $PYTHON "$SCRIPT_DIR/digest_pipeline/digest.py" --config "$CONFIG" $EXTRA_ARGS 2>&1 || DIGEST_EXIT=$?
fi

if [ "$DIGEST_EXIT" -eq 10 ] || [ "$DIGEST_EXIT" -eq 11 ]; then
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Digest skipped, not running podcast (exit=$DIGEST_EXIT)"
    exit 0
fi

if [ "$DIGEST_EXIT" -ne 0 ]; then
    exit "$DIGEST_EXIT"
fi

if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON="${PYTHON:-$SCRIPT_DIR/.venv/bin/python3}"
else
    PYTHON="${PYTHON:-python3}"
fi

$PYTHON "$SCRIPT_DIR/digest_pipeline/podcast.py" --config "$CONFIG" $EXTRA_ARGS 2>&1 || echo "Podcast generation failed (non-fatal)"

echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Pipeline complete"
