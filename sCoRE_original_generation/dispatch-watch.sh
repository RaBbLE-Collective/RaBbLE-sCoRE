#!/usr/bin/env bash
# =============================================================================
# RaBbLE-Collective — dispatch-watch.sh
# Watches tasks/pending/ with inotify and routes new task files to the
# correct agent tmux session via send-keys.
# Runs as its own persistent tmux session (rabble-dispatch).
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PENDING="$ROOT_DIR/tasks/pending"
ACTIVE="$ROOT_DIR/tasks/active"
DONE="$ROOT_DIR/tasks/done"

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; RESET='\033[0m'

log()  { echo -e "${CYAN}[dispatch]${RESET} $(date '+%H:%M:%S') $*"; }
ok()   { echo -e "${GREEN}[dispatch]${RESET} $(date '+%H:%M:%S') $*"; }
warn() { echo -e "${YELLOW}[dispatch]${RESET} $(date '+%H:%M:%S') $*"; }
err()  { echo -e "${RED}[dispatch]${RESET} $(date '+%H:%M:%S') $*" >&2; }

# Extract the 'agent:' field from a task file
get_agent() {
  grep -m1 '^agent:' "$1" | awk '{print $2}' | tr -d ' '
}

# Extract the task ID from filename: TASK-0042-search.md -> 0042
get_task_id() {
  basename "$1" | grep -oP '(?<=TASK-)\d+' || echo "unknown"
}

# Route a task file to the appropriate agent session
dispatch_task() {
  local task_file="$1"
  local filename
  filename="$(basename "$task_file")"
  local agent
  agent="$(get_agent "$task_file")"
  local task_id
  task_id="$(get_task_id "$task_file")"

  # Validate agent
  case "$agent" in
    memory|search|execution) ;;
    *)
      warn "Unknown agent '$agent' in $filename — moving to archive"
      mv "$task_file" "$ROOT_DIR/tasks/archive/$filename"
      return
      ;;
  esac

  local session="rabble-$agent"

  # Check session is alive
  if ! tmux has-session -t "$session" 2>/dev/null; then
    err "Agent session '$session' not running — task queued, retry in 10s"
    sleep 10
    dispatch_task "$task_file"
    return
  fi

  # Move to active (marks in-flight)
  mv "$task_file" "$ACTIVE/$filename"
  log "Dispatching TASK-$task_id to $session"

  # Inject task into agent session
  # Agent reads the file path and processes it
  tmux send-keys -t "$session" \
    "Process this task file: $ACTIVE/$filename — write result to $DONE/" Enter

  ok "TASK-$task_id → $session"
}

# Watch for completed tasks and notify sCoRE
watch_done() {
  inotifywait -m -e close_write,moved_to "$DONE" --format '%f' 2>/dev/null \
  | while read -r filename; do
      # Only notify for agent result files, not sCoRE writes
      if [[ "$filename" =~ -result\.md$ ]]; then
        local task_id
        task_id="$(echo "$filename" | grep -oP '(?<=TASK-)\d+' || echo "unknown")"
        log "Result ready: $filename — notifying sCoRE"

        if tmux has-session -t "rabble-score" 2>/dev/null; then
          tmux send-keys -t "rabble-score" \
            "Task result ready: $DONE/$filename" Enter
        fi

        # Move source from active/ to done/ tracking
        local active_match
        active_match=$(find "$ACTIVE" -name "TASK-${task_id}-*.md" 2>/dev/null | head -1)
        if [[ -n "$active_match" ]]; then
          mv "$active_match" "$ROOT_DIR/tasks/archive/$(basename "$active_match")"
        fi
      fi
    done
}

# Main watch loop
main() {
  log "RaBbLE dispatch watcher started"
  log "Watching: $PENDING"

  # Handle any tasks that were pending before we started
  for existing in "$PENDING"/TASK-*.md; do
    [[ -f "$existing" ]] && dispatch_task "$existing"
  done

  # Start done-watcher in background
  watch_done &
  local watch_done_pid=$!

  # Primary watch: new task files in pending/
  inotifywait -m -e close_write,moved_to "$PENDING" --format '%f' 2>/dev/null \
  | while read -r filename; do
      local task_file="$PENDING/$filename"
      if [[ -f "$task_file" && "$filename" =~ ^TASK-.*\.md$ ]]; then
        dispatch_task "$task_file"
      fi
    done

  # Cleanup background watcher on exit
  kill "$watch_done_pid" 2>/dev/null || true
}

main
