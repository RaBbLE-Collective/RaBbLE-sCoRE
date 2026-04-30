#!/usr/bin/env bash
# rabble-shell.sh — RaBbLE interactive shell
# The user-facing entity is RaBbLE. sCoRE is the internal coordination engine.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

C='\033[1;36m'
Y='\033[1;33m'
D='\033[2m'
B='\033[1m'
R='\033[0m'

# ── Spinner ────────────────────────────────────────────────────────────────────
SPINNER_PID=""
SPINNER_FRAMES=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

spinner_start() {
  local label="${1:-thinking}"
  (
    local i=0
    while true; do
      printf "\r${C}${SPINNER_FRAMES[$i]}${R} ${D}${label}...${R}" >&2
      i=$(( (i + 1) % ${#SPINNER_FRAMES[@]} ))
      sleep 0.08
    done
  ) &
  SPINNER_PID=$!
}

spinner_stop() {
  if [[ -n "$SPINNER_PID" ]]; then
    kill "$SPINNER_PID" 2>/dev/null || true
    wait "$SPINNER_PID" 2>/dev/null || true
    SPINNER_PID=""
    printf "\r\033[K" >&2
  fi
}

task_list() {
  find "$REPO_ROOT/tasks/pending" "$REPO_ROOT/tasks/active" \
    -name "TASK-*.md" 2>/dev/null | sort || true
}

trap 'spinner_stop; exit 0' EXIT INT TERM

# ── Banner ─────────────────────────────────────────────────────────────────────
clear

printf "${C}"
cat << 'BANNER'
 ____  ____  ____  ____  __    ____
(  _ \(  _ \(  _ \(  _ \(  )  ( ___)
 )   / )(_) ))___/ ) _ < )(__  )__)
(_)\_)(____/(__)  (____/(____)(____)
BANNER
printf "${R}"
printf "${D}  Recursive and Boundless Behavioral Learning Engine\n"
printf "  Coordination Intelligence · Epoch 0${R}\n\n"

# ── Ensure dispatch is running ─────────────────────────────────────────────────
if ! tmux has-session -t rabble-dispatch 2>/dev/null; then
  printf "${D}  [dispatch] starting...${R}\n"
  tmux new-session -d -s rabble-dispatch -c "$REPO_ROOT" \
    "bash scripts/dispatch-watch.sh"
  printf "${D}  [dispatch] ready${R}\n"
else
  printf "${D}  [dispatch] running${R}\n"
fi

# ── Load phase — warm up Claude before allowing prompts ───────────────────────
spinner_start "loading sCoRE"

WARMUP_PROMPT="You are now online. Read CLAUDE.md, check tasks/active/, tasks/pending/, and memory/context-snapshots/. Then respond with a single-line status: 'RaBbLE-sCoRE online. Active: N | Pending: N | [snapshot summary or \"no prior context\"]'"

WARMUP_RESPONSE=$(printf '%s\n' "$WARMUP_PROMPT" \
  | claude --dangerously-skip-permissions -p 2>/dev/null)

spinner_stop
printf "${D}  [sCoRE]    ready${R}\n\n"

# ── State report ───────────────────────────────────────────────────────────────
printf "${D}"
bash "$REPO_ROOT/scripts/hooks/session-start.sh" 2>/dev/null || true
printf "${R}\n"

# Print the warmup/greeting from sCoRE
printf "${C}RaBbLE${R}\n${WARMUP_RESPONSE}\n\n"

# ── REPL ───────────────────────────────────────────────────────────────────────
while true; do
  printf "${C}RaBbLE${R} ${D}▸${R} "
  IFS= read -r user_input || { printf "\n"; break; }
  [[ -z "$user_input" ]] && continue
  [[ "$user_input" == "exit" || "$user_input" == "quit" ]] && break

  printf "\n"
  before=$(task_list)

  spinner_start "thinking"
  RESPONSE=$(printf '%s\n' "$user_input" \
    | claude --dangerously-skip-permissions --continue -p 2>/dev/null)
  spinner_stop

  printf "${RESPONSE}\n\n"

  # Show dispatch activity if task files appeared
  sleep 0.3
  after=$(task_list)
  if [[ "$before" != "$after" ]]; then
    comm -13 \
      <(printf '%s\n' "$before" | sort) \
      <(printf '%s\n' "$after"  | sort) \
    | while IFS= read -r f; do
        [[ -f "$f" ]] || continue
        agent=$(grep -m1 '^agent:' "$f" 2>/dev/null | awk '{print $2}' || echo "?")
        printf "  ${Y}⇢${R} ${D}dispatched $(basename "$f") → ${agent}${R}\n"
      done
    printf "\n"
  fi
done
