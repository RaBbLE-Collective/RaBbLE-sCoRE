#!/usr/bin/env bash
# Graceful shutdown of all RaBbLE-sCoRE tmux sessions

set -euo pipefail

SESSIONS=(rabble-score rabble-dispatch rabble-memory rabble-search rabble-execution)

for session in "${SESSIONS[@]}"; do
  if tmux has-session -t "$session" 2>/dev/null; then
    tmux kill-session -t "$session"
    echo "[sCoRE] Stopped: $session"
  fi
done

echo "[sCoRE] All sessions stopped"
