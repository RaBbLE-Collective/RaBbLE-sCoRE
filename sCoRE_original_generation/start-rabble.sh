#!/usr/bin/env bash
# =============================================================================
# RaBbLE-Collective — start-rabble.sh
# Bootstraps all agent tmux sessions and inotify dispatch watchers.
# Usage: ./scripts/start-rabble.sh [--restart]
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Colors ------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

log()  { echo -e "${CYAN}[RaBbLE]${RESET} $*"; }
ok()   { echo -e "${GREEN}[  OK  ]${RESET} $*"; }
warn() { echo -e "${YELLOW}[ WARN ]${RESET} $*"; }
err()  { echo -e "${RED}[ ERR  ]${RESET} $*" >&2; }

# --- Dependency check ---------------------------------------------------------
check_deps() {
  local missing=()
  for cmd in tmux claude inotifywait; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    err "Missing dependencies: ${missing[*]}"
    echo "  Install on Fedora: sudo dnf install tmux inotify-tools"
    echo "  Claude Code CLI:   npm install -g @anthropic-ai/claude-code"
    exit 1
  fi
}

# --- Session helpers ----------------------------------------------------------
session_exists() { tmux has-session -t "$1" 2>/dev/null; }

kill_session() {
  if session_exists "$1"; then
    tmux kill-session -t "$1"
    warn "Killed existing session: $1"
  fi
}

start_agent_session() {
  local session="$1"
  local agent_file="$2"
  local restart="${3:-false}"

  if session_exists "$session"; then
    if [[ "$restart" == "true" ]]; then
      kill_session "$session"
    else
      warn "Session '$session' already running — skipping (use --restart to force)"
      return
    fi
  fi

  tmux new-session -d -s "$session" -c "$ROOT_DIR"
  # Load agent with its system prompt, persist across disconnects
  tmux send-keys -t "$session" \
    "claude --system-prompt '$ROOT_DIR/$agent_file' --continue" Enter
  ok "Started agent session: $session"
}

# --- inotify dispatcher -------------------------------------------------------
start_dispatcher() {
  local restart="${1:-false}"

  if session_exists "rabble-dispatch"; then
    if [[ "$restart" == "true" ]]; then
      kill_session "rabble-dispatch"
    else
      warn "Dispatch watcher already running — skipping"
      return
    fi
  fi

  tmux new-session -d -s "rabble-dispatch" -c "$ROOT_DIR"
  tmux send-keys -t "rabble-dispatch" \
    "scripts/dispatch-watch.sh" Enter
  ok "Started inotify dispatch watcher: rabble-dispatch"
}

# --- sCoRE session ------------------------------------------------------------
start_score() {
  local restart="${1:-false}"

  if session_exists "rabble-score"; then
    if [[ "$restart" == "true" ]]; then
      kill_session "rabble-score"
    else
      warn "sCoRE session already running — skipping"
      return
    fi
  fi

  tmux new-session -d -s "rabble-score" -c "$ROOT_DIR"
  tmux send-keys -t "rabble-score" \
    "claude --system-prompt '$ROOT_DIR/agents/score.md' --continue" Enter
  ok "Started sCoRE session: rabble-score"
}

# --- Main --------------------------------------------------------------------
main() {
  local restart=false
  [[ "${1:-}" == "--restart" ]] && restart=true

  log "Bootstrapping RaBbLE-Collective from $ROOT_DIR"
  echo ""

  check_deps

  # Ensure task dirs exist
  mkdir -p "$ROOT_DIR/tasks"/{pending,active,done,archive}
  mkdir -p "$ROOT_DIR/memory"/{patterns,context-snapshots}

  # Start agent sessions
  start_agent_session "rabble-memory"    "agents/memory.md"    "$restart"
  start_agent_session "rabble-search"    "agents/search.md"    "$restart"
  start_agent_session "rabble-execution" "agents/execution.md" "$restart"

  # Start task dispatcher (inotify watcher)
  start_dispatcher "$restart"

  # Start sCoRE last (it may immediately check for pending tasks)
  start_score "$restart"

  echo ""
  log "All sessions started. Summary:"
  echo ""
  tmux list-sessions | grep "rabble" | while read -r line; do
    echo -e "  ${GREEN}●${RESET} $line"
  done
  echo ""
  log "Attach to sCoRE:      tmux attach -t rabble-score"
  log "Watch dispatcher:     tmux attach -t rabble-dispatch"
  log "View all sessions:    tmux ls | grep rabble"
  log "Stop everything:      scripts/stop-rabble.sh"
}

main "${@}"
