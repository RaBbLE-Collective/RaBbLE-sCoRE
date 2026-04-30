#!/usr/bin/env bash
# rabble-dispatch — inotify watcher that routes task files to agent sessions
# Watches tasks/pending/, spawns agent sessions for incoming tasks

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PENDING="$REPO_ROOT/tasks/pending"
ACTIVE="$REPO_ROOT/tasks/active"

echo "[dispatch] Watching $PENDING for new tasks..."

inotifywait -m -e close_write --format '%f' "$PENDING" | while read -r filename; do
  [[ "$filename" == *.md ]] || continue
  [[ "$filename" == TASK-* ]] || continue

  task_file="$PENDING/$filename"
  agent=$(grep -m1 '^agent:' "$task_file" | awk '{print $2}' || echo "")

  if [[ -z "$agent" ]]; then
    echo "[dispatch] WARN: no agent field in $filename — skipping"
    continue
  fi

  session="rabble-$agent"
  echo "[dispatch] Routing $filename → $session"

  mv "$task_file" "$ACTIVE/$filename"

  if ! tmux has-session -t "$session" 2>/dev/null; then
    tmux new-session -d -s "$session" -c "$REPO_ROOT" \
      "claude --dangerously-skip-permissions"
    sleep 0.5
  fi

  tmux send-keys -t "$session" \
    "Process task file: $ACTIVE/$filename — read it, execute the objective, write result to tasks/done/$(basename "$filename" .md)-result.md, then move the task to tasks/archive/" \
    Enter

  echo "[dispatch] Dispatched $filename to $session"
done
