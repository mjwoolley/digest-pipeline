#!/bin/bash
set -euo pipefail

echo "Setting up digest-pipeline..."

# Check Python version
python3 -c "import sys; assert sys.version_info >= (3,11), f'Python 3.11+ required, got {sys.version}'" || exit 1

# Create venv & install
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$SCRIPT_DIR/.venv"
fi
source "$SCRIPT_DIR/.venv/bin/activate"
echo "Installing digest-pipeline..."
pip install -e "$SCRIPT_DIR"

# Detect package manager
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v brew &>/dev/null; then
    PKG_MGR="brew"
else
    PKG_MGR=""
fi

# Helper: prompt to install a missing tool
install_tool() {
    local tool="$1" cmd="$2" note="${3:-}"
    if ! command -v "$tool" &>/dev/null; then
        echo ""
        [ -n "$note" ] && echo "  ($note)"
        read -p "'$tool' not found. Install with: $cmd ? [y/N] " answer
        if [[ "$answer" =~ ^[Yy] ]]; then
            eval "$cmd"
        else
            echo "  Skipped $tool"
        fi
    fi
}

# System dependencies
if [ "$PKG_MGR" = "apt" ]; then
    install_tool "ffmpeg" "sudo apt-get install -y ffmpeg" "Required for podcast MP3 generation"
    install_tool "curl" "sudo apt-get install -y curl" "Required for fetching RSS feeds"
elif [ "$PKG_MGR" = "brew" ]; then
    install_tool "ffmpeg" "brew install ffmpeg" "Required for podcast MP3 generation"
    install_tool "curl" "brew install curl" "Required for fetching RSS feeds"
fi

# bird (npm package, optional for Twitter)
install_tool "bird" "npm install -g bird" "Optional — only needed for Twitter/X sources"

echo ""
echo "Setup complete! Activate with: source .venv/bin/activate"
