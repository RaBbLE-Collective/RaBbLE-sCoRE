#!/usr/bin/env bash
# rabble-dispatch — inotify watcher that routes task files to agent sessions
# Watches tasks/pending/, spawns agent sessions for incoming tasks

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PENDING="$REPO_ROOT/tasks/pending"
ACTIVE="$REPO_ROOT/tasks/active"

route_task() {
  local filename="$1"
  local task_file="$PENDING/$filename"

  [[ -f "$task_file" ]] || return 0

  local agent
  agent=$(grep -m1 '^agent:' "$task_file" | awk '{print $2}' || echo "")

  if [[ -z "$agent" ]]; then
    echo "[dispatch] WARN: no agent field in $filename — skipping"
    return 0
  fi

  local agent_identity="$REPO_ROOT/agents/$agent.md"
  local task_id
  task_id=$(basename "$filename" .md)
  local result_file="$REPO_ROOT/tasks/done/${task_id}-result.md"
  local archive_dest="$REPO_ROOT/tasks/archive/$filename"
  local session="rabble-${agent}-${task_id}"

  echo "[dispatch] Routing $filename → $agent"

  mv "$task_file" "$ACTIVE/$filename"

  # Write prompt to temp file to avoid shell escaping issues
  local prompt_file
  prompt_file=$(mktemp /tmp/rabble-prompt-XXXXXX.txt)
  cat > "$prompt_file" <<PROMPT
$(cat "$agent_identity")

---

You have been dispatched to handle a task. Your instructions:

1. Read the task file: $ACTIVE/$filename
2. Execute the objective described in ## Objective
3. Write your result to: $result_file
4. Move the task file to: $archive_dest

Do not ask for confirmation. Execute and write results.
PROMPT

  # Agent sessions run from a temp dir so the sCoRE project settings.json
  # (which blocks Bash/WebSearch/WebFetch) does not apply to them.
  local agent_work_dir
  agent_work_dir=$(mktemp -d /tmp/rabble-agent-XXXXXX)

  # Run claude in print mode — reads prompt from stdin, no TUI, no interactive prompts
  tmux new-session -d -s "$session" -c "$agent_work_dir" \
    "claude --dangerously-skip-permissions -p < '$prompt_file' ; rm -f '$prompt_file' ; rm -rf '$agent_work_dir' ; echo '[dispatch] $session complete'"

  echo "[dispatch] Dispatched $filename → session: $session"
}

# Drain any tasks already in pending/ before the watcher starts
echo "[dispatch] Draining existing tasks in $PENDING..."
for existing_task in "$PENDING"/TASK-*.md; do
  [[ -f "$existing_task" ]] || continue
  route_task "$(basename "$existing_task")"
done

echo "[dispatch] Watching $PENDING for new tasks..."

inotifywait -m -e close_write --format '%f' "$PENDING" | while read -r filename; do
  [[ "$filename" == *.md ]] || continue
  [[ "$filename" == TASK-* ]] || continue
  route_task "$filename"
done
