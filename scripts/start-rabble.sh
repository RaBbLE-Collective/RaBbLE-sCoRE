#!/usr/bin/env bash
# Bootstrap all RaBbLE-sCoRE sessions in tmux
# Flags: --restart  kill existing sessions before starting
#        --with-server  also start the FastAPI web server on :8000

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESTART=false
WITH_SERVER=false

for arg in "$@"; do
  [[ "$arg" == "--restart" ]]     && RESTART=true
  [[ "$arg" == "--with-server" ]] && WITH_SERVER=true
done

if $RESTART; then
  echo "[sCoRE] Stopping existing sessions..."
  bash "$REPO_ROOT/scripts/stop-rabble.sh" || true
fi

echo "[sCoRE] Starting dispatch watcher..."
tmux new-session -d -s rabble-dispatch -c "$REPO_ROOT" \
  "bash scripts/dispatch-watch.sh" 2>/dev/null || \
  echo "[sCoRE] rabble-dispatch already running"

echo "[sCoRE] Starting sCoRE orchestrator..."
tmux new-session -d -s rabble-score -c "$REPO_ROOT" \
  "bash scripts/rabble-shell.sh" 2>/dev/null || \
  echo "[sCoRE] rabble-score already running"

if $WITH_SERVER; then
  echo "[sCoRE] Starting web server on :8000..."
  tmux new-session -d -s rabble-server -c "$REPO_ROOT/server" \
    "uvicorn main:app --host 0.0.0.0 --port 8000 --reload" 2>/dev/null || \
    echo "[sCoRE] rabble-server already running"
fi

echo ""
echo "[sCoRE] Sessions:"
tmux list-sessions 2>/dev/null | grep -E "rabble-(score|dispatch|memory|search|execution|server)" || true
echo ""
echo "[sCoRE] Attach: tmux attach -t rabble-score"
if $WITH_SERVER; then
  echo "[sCoRE] Server:  http://localhost:8000/health"
fi
