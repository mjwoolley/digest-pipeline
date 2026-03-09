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

# Prefer installed CLI, fall back to module execution
if command -v digest-pipeline &>/dev/null; then
    digest-pipeline "$CONFIG" $EXTRA_ARGS 2>&1
else
    # Use local .venv python if available, otherwise system python3
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
        PYTHON="$SCRIPT_DIR/.venv/bin/python3"
    else
        PYTHON="python3"
    fi

    $PYTHON "$SCRIPT_DIR/digest_pipeline/digest.py" --config "$CONFIG" $EXTRA_ARGS 2>&1

    $PYTHON "$SCRIPT_DIR/digest_pipeline/podcast.py" --config "$CONFIG" $EXTRA_ARGS 2>&1 || echo "Podcast generation failed (non-fatal)"
fi

echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Pipeline complete"
