#!/usr/bin/env bash
# status.sh — show RaBbLE system health at a glance

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

C='\033[1;36m'
G='\033[1;32m'
Y='\033[1;33m'
RED='\033[1;31m'
D='\033[2m'
R='\033[0m'

session_status() {
  local name="$1"
  if tmux has-session -t "$name" 2>/dev/null; then
    printf "${G}●${R} $name\n"
  else
    printf "${RED}○${R} $name ${D}(not running)${R}\n"
  fi
}

task_count() {
  local dir="$1"
  find "$dir" -name "TASK-*.md" 2>/dev/null | wc -l | tr -d ' '
}

printf "\n${C}RaBbLE System Status${R}\n"
printf "${D}$(date '+%Y-%m-%d %H:%M:%S')${R}\n\n"

printf "${D}── Sessions ──────────────────────────────────${R}\n"
session_status "rabble-dispatch"
session_status "rabble-score"
session_status "rabble-server"
printf "\n"

printf "${D}── Task Pipeline ─────────────────────────────${R}\n"
pending=$(task_count "$REPO_ROOT/tasks/pending")
active=$(task_count "$REPO_ROOT/tasks/active")
done_count=$(task_count "$REPO_ROOT/tasks/done")
archive=$(task_count "$REPO_ROOT/tasks/archive")

[[ "$pending" -gt 0 ]] && printf "${Y}  pending:${R}  $pending\n" || printf "${D}  pending:  $pending${R}\n"
[[ "$active"  -gt 0 ]] && printf "${Y}  active:${R}   $active\n"  || printf "${D}  active:   $active${R}\n"
printf "${D}  done:     $done_count\n"
printf "  archive:  $archive${R}\n"
printf "\n"

if [[ "$pending" -gt 0 ]]; then
  printf "${D}── Pending Tasks ─────────────────────────────${R}\n"
  find "$REPO_ROOT/tasks/pending" -name "TASK-*.md" | sort | while read -r f; do
    agent=$(grep -m1 '^agent:' "$f" | awk '{print $2}' || echo "?")
    printf "  ${Y}$(basename "$f")${R} ${D}→ $agent${R}\n"
  done
  printf "\n"
fi

if [[ "$active" -gt 0 ]]; then
  printf "${D}── Active Tasks ──────────────────────────────${R}\n"
  find "$REPO_ROOT/tasks/active" -name "TASK-*.md" | sort | while read -r f; do
    agent=$(grep -m1 '^agent:' "$f" | awk '{print $2}' || echo "?")
    printf "  ${C}$(basename "$f")${R} ${D}→ $agent${R}\n"
  done
  printf "\n"
fi

printf "${D}── Web Server ────────────────────────────────${R}\n"
if tmux has-session -t "rabble-server" 2>/dev/null; then
  health=$(curl -sf http://localhost:8000/health 2>/dev/null || echo "unreachable")
  printf "  ${G}●${R} rabble-server ${D}→ $health${R}\n"
else
  printf "  ${D}○ rabble-server (not running — use --with-server to start)${R}\n"
fi
printf "\n"

printf "${D}── Dispatch Log (last 10 lines) ──────────────${R}\n"
if tmux has-session -t "rabble-dispatch" 2>/dev/null; then
  tmux capture-pane -t rabble-dispatch -p 2>/dev/null | tail -10 | sed 's/^/  /'
else
  printf "  ${D}dispatch not running${R}\n"
fi
printf "\n"
