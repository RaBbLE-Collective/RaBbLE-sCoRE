# CONTEXT.md — RaBbLE-sCoRE

```
epoch: 0 | episode: 1 | status: active
```

RaBbLE-sCoRE is the coordination server of the RaBbLE Collective.
Intent → decompose → delegate → result. No execution surface of its own.

---

## What Is Done

### Epoch 0 — Foundation (Episode 1)

| Item | Status |
|---|---|
| Repo initialized at `/home/rabble/RaBbLE/RaBbLE-sCoRE/` | Done |
| Clean `main` scaffold commit (Pulse Protocol style) | Done |
| `episode-1` branch created | Done |
| Full directory scaffold: agents, tasks, memory, scripts, grimoire, projects | Done |
| `README.md` — project overview, quick start, directory map | Done |
| `AGENT.md` — LLM agent entry point | Done |
| `.claude/settings.json` — Bash/Web blocked at tool level in sCoRE session | Done |
| `agents/score.md` — sCoRE orchestrator identity | Done |
| `agents/memory.md` — rabble-memory agent identity | Done |
| `agents/search.md` — rabble-search agent identity | Done |
| `agents/execution.md` — rabble-execution agent identity | Done |
| `scripts/dispatch-watch.sh` — inotify watcher, routes tasks to agents | Done |
| `scripts/start-rabble.sh` — bootstrap all tmux sessions | Done |
| `scripts/stop-rabble.sh` — graceful shutdown | Done |
| `scripts/hooks/session-start.sh` — sCoRE orientation on startup | Done |
| `scripts/hooks/post-write.sh` — task file validator | Done |
| `grimoire/RaBbLE-sCoRE-Architecture.md` — full system design | Done |
| `grimoire/RaBbLE-sCoRE-Roadmap.md` — episode/epoch trajectory | Done |
| `memory/patterns/dispatch-routing.md` — first pattern | Done |
| **First live dispatch round-trip verified** | Done |
| `memory/patterns/dispatch-print-mode.md` — learned: use `-p` not interactive | Done |
| `tasks/done/TASK-0001-search-result.md` — first real agent result | Done |

### Key Discovery from Episode 1

Dispatch must use `claude -p < prompt_file` (print mode), not interactive `claude` + `send-keys`.
Interactive mode fails because the TUI trust/bypass prompts absorb keystrokes before Claude is ready.
Print mode skips all TUI. Agent identity injected at top of prompt via temp file.
Full pattern in `memory/patterns/dispatch-print-mode.md`.

---

## What Is Not Done Yet

### Episode 1 — Remaining

| Item | Notes |
|---|---|
| `start-rabble.sh` verified on live machine end-to-end | Scripts written, dispatch tested manually — full bootstrap script untested |
| GitHub remote added | Skipped by choice, will do later |
| Episode 1 Echo tag | Tag `echo-1.0` on `episode-1` when bootstrap verified |

### Episode 2 — sCoRE Live Session

| Item | Notes |
|---|---|
| sCoRE runs in its own constrained Claude Code session | Session reads `agents/score.md`, `.claude/settings.json` blocks Bash/Web |
| sCoRE receives user intent and writes a task file | The actual coordination loop: human → sCoRE → task file → dispatch |
| sCoRE reads result from `tasks/done/` and responds | Closing the loop through sCoRE reasoning, not just dispatch mechanics |
| Execution agent round-trip | TASK-0001 used search. Need an execution round-trip too |

### Episode 3 and Beyond

| Item | Notes |
|---|---|
| Memory agent wired into sCoRE decisions | sCoRE reads `memory/patterns/` before delegating |
| `start-rabble.sh` starts sCoRE session (not just dispatch) | Currently starts dispatch only; sCoRE session still manual |
| FastAPI server layer (optional) | For HTTP interface to sCoRE — deferred until the loop is solid |
| GitHub remote + CI | Deferred |
| Collective registry entry for sCoRE | Register in `RaBbLE-Collective/registry/manifests/` |

---

## Active Branch

`episode-1` — 5 commits ahead of `main`

```
8b0e9bb transcribe ~ memory >> dispatch print-mode pattern logged
ef8e75a mend ~ dispatch >> switch to claude -p print mode
be7c05f spark ~ tasks >> TASK-0001 seed + dispatch routing pattern
56203ce spark ~ scripts >> dispatch wired and executable
f54df1f [main] spark ~ sCoRE >> substrate initialized
```

---

## How to Test sCoRE Right Now

### 1. Check dispatch is alive

```bash
cd /home/rabble/RaBbLE/RaBbLE-sCoRE
tmux list-sessions | grep rabble
```

If `rabble-dispatch` is not listed:
```bash
tmux new-session -d -s rabble-dispatch -c "$(pwd)" "bash scripts/dispatch-watch.sh"
```

### 2. Write a test task

```bash
cat > tasks/pending/TASK-0002-execution.md << 'EOF'
# Task 0002
agent: execution
priority: low
created: 2026-04-29T00:00:00Z
context_refs: []

## Objective
Run `uname -a` and `free -h` and return the output.

## Input
No input needed beyond the commands above.

## Output format
Plain output from each command, labeled.

## Success criteria
Both commands ran, exit code 0, output returned.
EOF
```

### 3. Watch the round-trip

```bash
# Watch dispatch
tmux capture-pane -t rabble-dispatch -p

# Watch task lifecycle
watch -n1 'ls tasks/pending/ tasks/active/ tasks/done/'
```

### 4. Read the result

```bash
cat tasks/done/TASK-0002-execution-result.md
```

### 5. Check overall status

```bash
echo "=== Sessions ===" && tmux list-sessions 2>/dev/null || echo "none"
echo "=== Pending ===" && ls tasks/pending/
echo "=== Active ===" && ls tasks/active/
echo "=== Done ===" && ls tasks/done/
echo "=== Patterns ===" && ls memory/patterns/
```

---

## Reading Order for a New Session

1. This file (`CONTEXT.md`) — you are here
2. `AGENT.md` — rules and workspace map
3. `agents/score.md` — sCoRE identity (what sCoRE IS)
4. `grimoire/RaBbLE-sCoRE-Architecture.md` — system design
5. `memory/patterns/dispatch-print-mode.md` — the critical dispatch lesson
6. `grimoire/RaBbLE-sCoRE-Roadmap.md` — episode/epoch plan

For Collective context: `../RaBbLE-Collective/grimoire/RaBbLE-Collective.md`
