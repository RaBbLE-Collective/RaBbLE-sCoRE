#!/usr/bin/env bash
# Bootstrap all RaBbLE-sCoRE sessions in tmux

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESTART=false

[[ "${1:-}" == "--restart" ]] && RESTART=true

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

echo ""
echo "[sCoRE] Sessions:"
tmux list-sessions 2>/dev/null | grep -E "rabble-(score|dispatch|memory|search|execution)" || true
echo ""
echo "[sCoRE] Attach: tmux attach -t rabble-score"
