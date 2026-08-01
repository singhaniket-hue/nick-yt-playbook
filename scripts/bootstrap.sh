#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
  echo "Syncing the Python 3.11+ environment with uv ..."
  uv sync --project "$ROOT" --extra dev
  RUN_COMMAND="uv run --project \"$ROOT\" rabbithole"
else
  PYTHON="${PYTHON:-python3}"
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "uv was not found and Python executable '$PYTHON' is unavailable." >&2
    echo "Install uv or Python 3.11+, or set PYTHON=/path/to/python." >&2
    exit 1
  fi
  "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'
  if [[ ! -x ".venv/bin/python" ]]; then
    echo "Creating .venv with $PYTHON ..."
    "$PYTHON" -m venv .venv
  fi
  .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Existing .venv uses Python older than 3.11; recreate it")'
  .venv/bin/python -m ensurepip --upgrade
  .venv/bin/python -m pip install -e '.[dev]'
  # Keep the environment's yt-dlp entry point visible to child processes even
  # though this script cannot activate the caller's shell.
  RUN_COMMAND="PATH=\"$ROOT/.venv/bin:\$PATH\" \"$ROOT/.venv/bin/python\" -m rabbithole.cli"
fi

if [[ ! -f ".env" ]]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi

echo "Bootstrap complete. No application was launched and no render queue was touched."
echo "Add ELEVENLABS_API_KEY and RABBITHOLE_VOICE_ID to .env only if needed."
echo "Prepared-project Resolve host: Resolve plus FFmpeg/ffprobe with libass; no browser, Poppler, or API key is needed."
echo "End-to-end acquisition host: also install Chrome/Edge/Chromium and Poppler's pdftoppm."
echo "Run: $RUN_COMMAND resolve doctor --mode free"
echo 'macOS full-pipeline tools: brew install ffmpeg-full poppler && export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"'
echo 'macOS browser (only when Chrome/Edge is absent): brew install --cask google-chrome'
