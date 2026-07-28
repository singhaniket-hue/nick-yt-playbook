#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
  echo "Syncing the Python 3.11+ environment with uv ..."
  uv sync --extra dev
else
  PYTHON="${PYTHON:-python3}"
  "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
  if [[ ! -x ".venv/bin/python" ]]; then
    echo "Creating .venv with $PYTHON ..."
    "$PYTHON" -m venv .venv
  fi
  .venv/bin/python -m ensurepip --upgrade
  .venv/bin/python -m pip install -e '.[dev]'
fi

if [[ ! -f ".env" ]]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi

echo "Bootstrap complete. No application was launched and no render queue was touched."
echo "Add ELEVENLABS_API_KEY and RABBITHOLE_VOICE_ID to .env only if needed."
echo "Run: uv run rabbithole resolve doctor"
echo "macOS prerequisites: DaVinci Resolve 21+, FFmpeg/ffprobe, and optionally Poppler plus Chrome/Edge."
