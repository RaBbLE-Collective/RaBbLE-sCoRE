#!/usr/bin/env bash
# Cast: bash spells/local-start.sh
# Starts sCoRE locally for dev. Prerequisites:
#   - server/.env exists (copy from env.defaults + fill in API keys)
#   - Claude Code local server is running (for Haiku via fast tier)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/../server"

cd "$SERVER_DIR"

if [ ! -f .env ]; then
  echo "[sCoRE] No server/.env found. Copy env.defaults → .env and fill in values."
  exit 1
fi

echo "[sCoRE] Installing dependencies..."
pip install -q -r requirements.txt

echo "[sCoRE] Starting server on port ${PORT:-8000}..."
exec uvicorn main:app --reload --port "${PORT:-8000}"
