#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# harness/local.sh — start a RaBbLE service locally
#
# Usage:
#   ./harness/local.sh [service] [port]
#
# Examples:
#   ./harness/local.sh                   # intelligence on :8000
#   ./harness/local.sh intelligence 8001
#
# What it does:
#   1. Generates services/<svc>/.env from .secrets + env.defaults (if missing)
#   2. Creates services/<svc>/.venv (if missing)
#   3. Installs requirements.txt
#   4. Starts uvicorn with --reload
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HARNESS_DIR/.." && pwd)"

SERVICE="${1:-intelligence}"
PORT="${2:-8000}"

SERVICE_DIR="$ROOT/services/$SERVICE"
VENV_DIR="$SERVICE_DIR/.venv"
ENV_FILE="$SERVICE_DIR/.env"
REQ_FILE="$SERVICE_DIR/requirements.txt"

# ── Colours ────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[0;33m'
RED='\033[0;31m';   RESET='\033[0m'
ok()   { echo -e "${GREEN}✓${RESET}  $*"; }
info() { echo -e "${CYAN}·${RESET}  $*"; }
warn() { echo -e "${YELLOW}!${RESET}  $*"; }
err()  { echo -e "${RED}✗${RESET}  $*" >&2; }

# ── Validate ────────────────────────────────────────────────────────────────

if [ ! -d "$SERVICE_DIR" ]; then
  err "Service '$SERVICE' not found at $SERVICE_DIR"
  echo "  Available services:"
  ls "$ROOT/services/" 2>/dev/null | sed 's/^/    /'
  exit 1
fi

echo
echo -e "${CYAN}  RaBbLE · Local Service Runner${RESET}"
echo    "  Service : $SERVICE"
echo    "  Port    : $PORT"
echo    "  Dir     : $SERVICE_DIR"
echo

# ── Generate .env if missing ────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$ROOT/.secrets" ]; then
    info ".env not found — generating from .secrets ..."
    "$HARNESS_DIR/env_gen.sh" "$SERVICE"
  else
    warn ".env not found and .secrets not found."
    warn "Copy the template:  cp secrets.example .secrets"
    warn "Then fill in your API keys and re-run."
    echo
    if [ -f "$SERVICE_DIR/.env.example" ]; then
      warn "Falling back to .env.example for this run (LLM calls will fail without real keys)."
      cp "$SERVICE_DIR/.env.example" "$ENV_FILE"
    else
      err "No .env or .env.example found. Cannot start."
      exit 1
    fi
  fi
fi
ok ".env ready"

# ── Python venv ─────────────────────────────────────────────────────────────

if [ ! -d "$VENV_DIR" ]; then
  info "Creating Python venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
  ok "Venv created"
fi

PYTHON="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"
UVICORN="$VENV_DIR/bin/uvicorn"

# ── Dependencies ─────────────────────────────────────────────────────────────

if [ -f "$REQ_FILE" ]; then
  info "Installing / syncing requirements ..."
  "$PIP" install -q -r "$REQ_FILE"
  ok "Dependencies ready"
fi

# ── Start server ─────────────────────────────────────────────────────────────

echo
echo -e "${GREEN}  Starting RaBbLE · $SERVICE · http://localhost:$PORT${RESET}"
echo    "  API docs: http://localhost:$PORT/docs"
echo    "  Health:   http://localhost:$PORT/health"
echo    "  Ctrl+C to stop"
echo

# Source .env and export to uvicorn's environment
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$SERVICE_DIR"
exec "$UVICORN" main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --reload \
  --log-level info
