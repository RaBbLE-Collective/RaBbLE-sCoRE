# CONTEXT.md — RaBbLE-sCoRE

```
epoch: 0 | evolution: 0 | echo: 0 | episode: 0 (pending) | version: v0.0.0.0 | status: active
```

RaBbLE-sCoRE is the coordination server of the RaBbLE Collective.
Intent → decompose → delegate → result. No execution surface of its own.

---

## What Is Done (Pre-Episode-1 Events)

All work below is accumulated toward Episode 1 air (`v0.0.0.1`). Episode 1 has not yet aired.

### Episode 1, Plot A — First Breath

| Item | Status |
|---|---|
| Repo initialized at `RaBbLE-sCoRE/` | Done |
| Clean `main` scaffold commit (Pulse Protocol style) | Done |
| Full directory scaffold: agents, tasks, memory, scripts, grimoire, projects | Done |
| `README.md`, `AGENT.md` | Done |
| `.claude/settings.json` — Bash/Web blocked at tool level | Done |
| Agent identities: `agents/score.md`, `memory.md`, `search.md`, `execution.md` | Done |
| `scripts/dispatch-watch.sh` — inotify watcher, routes tasks to agents | Done |
| `scripts/start-rabble.sh`, `stop-rabble.sh` | Done |
| `scripts/hooks/session-start.sh`, `post-write.sh` | Done |
| `grimoire/RaBbLE-sCoRE-Architecture.md`, `Roadmap.md` | Done |
| `memory/patterns/dispatch-routing.md`, `dispatch-print-mode.md` | Done |
| First live dispatch round-trip verified (TASK-0001-search) | Done |
| Echo tag `echo-1.0` | Done |

**Key lesson from Episode 1:** Dispatch must use `claude -p < prompt_file`. Interactive TUI absorbs keystrokes before Claude is ready. Full pattern in `memory/patterns/dispatch-print-mode.md`.

---

### Episode 1, Plot B — sCoRE Live Session

#### What was built

| Item | File(s) | Notes |
|---|---|---|
| sCoRE identity injection | `CLAUDE.md` | `@agents/score.md` — loaded automatically by Claude Code on session start |
| Agent tool blocked in sCoRE | `.claude/settings.json` | Added `Agent(*)` to deny list — sCoRE must delegate via task files, not spawn subagents |
| Dispatch: agent isolation fix | `scripts/dispatch-watch.sh` | Agents run from `mktemp -d /tmp/rabble-agent-*` — project settings.json does not apply |
| Dispatch: startup queue drain | `scripts/dispatch-watch.sh` | Processes existing files in `tasks/pending/` on watcher startup — no missed tasks after restart |
| Dispatch: refactored | `scripts/dispatch-watch.sh` | Extracted `route_task()` function, used by both startup drain and inotify loop |
| RaBbLE interactive shell | `scripts/rabble-shell.sh` | Custom REPL — not Claude Code TUI. Loading phase, spinners, dispatch notifications |
| Start script updated | `scripts/start-rabble.sh` | Now launches `rabble-shell.sh` instead of raw `claude` |
| System health check | `scripts/status.sh` | Shows sessions, task pipeline counts, dispatch log |
| Agent isolation pattern | `memory/patterns/agent-isolation.md` | Documents the settings.json scope lesson |

#### RaBbLE shell startup sequence

```
1.  Banner + tagline
2.  [dispatch] check — starts it if not running
3.  ⠋ loading sCoRE... — cold start absorbed here (WARMUP_PROMPT sent, response = greeting)
4.  [sCoRE] ready
5.  State report (session-start.sh)
6.  RaBbLE: <greeting from sCoRE>
7.  RaBbLE ▸ <prompt>
```

Per turn: spinner spins while `claude --continue -p` processes. After response, checks if new task files appeared and prints `⇢ dispatched TASK-XXXX → agent`.

#### Key discoveries from Episode 2

**1. Project settings.json applies to ALL Claude sessions from REPO_ROOT.**
Dispatch agent sessions (execution, search) were inheriting sCoRE's Bash/Web deny rules.
Fix: run all agent sessions from a temp working dir. Full pattern in `memory/patterns/agent-isolation.md`.

**2. Claude Code Agent tool bypasses file-based delegation.**
When sCoRE tried to fulfill a request using the built-in `Agent` tool, it spawned subagents
that ALSO loaded CLAUDE.md and refused to use Bash. Fix: `Agent(*)` added to deny list in
`.claude/settings.json`. sCoRE's only delegation path is writing to `tasks/pending/`.

**3. The shell entity is RaBbLE, not sCoRE.**
sCoRE is the internal coordination engine. The user-facing interactive shell is branded as
RaBbLE. `score-shell.sh` is a redirect stub; `rabble-shell.sh` is the actual shell.

**4. Pre-warm during loading absorbs cold start.**
`claude -p` startup overhead is ~2–5s. Rather than making the user wait on first prompt,
the loading phase sends a warmup message and uses the response as the RaBbLE greeting.
All subsequent turns use `--continue` on the already-warm session.

---

---

### Episode 1, Plot C — Server Merged, Collective → Grimoire

#### What was absorbed

| Item | Notes |
|---|---|
| `server/` — FastAPI intelligence service | Moved from RaBbLE-Server. Provides HTTP transport for sCoRE as a web API. |
| `harness/` — deployment tooling | Moved from RaBbLE-Server. local.sh, deploy.sh, railway_ctl.sh, env_gen.sh. |
| `scripts/start-rabble.sh --with-server` | Optional flag to start uvicorn as `rabble-server` tmux session alongside sCoRE. |
| `scripts/status.sh` server block | Displays rabble-server session state + /health response. |
| RaBbLE-Server repo deprecated | CONTEXT.md updated to point here. |

sCoRE can now be run as a local coordination shell (default) **or** as a web API endpoint
(with `--with-server`). Server routes call LLM providers directly — task pipeline wiring is Episode 4+.

---

## What Is Not Done Yet

### Episode 1 Exit Conditions (current target)

| Item | Status |
|---|---|
| `harness/` path bug fixed (`server/` not `services/intelligence/`) | Done |
| `server/main.py` version aligned to `v0.0.0.0` | Done |
| `server/api_test.py` Python test client | Done |
| `harness/local.sh` starts server successfully | Needs verification |
| Render deploy live | Done — https://score-origin.invalid/health ✓ |
| `RaBbLE-Grimoire/spells/deploy-score.sh` spell | Pending |
| Episode 1 tagged on `main` | Pending — airs when Collective Episode 1 ready |

### Episode 2 and Beyond (after Episode 1 airs)

| Item | Notes |
|---|---|
| Full coordination loop E2E verified | RaBbLE shell built; execution round-trip needs clean test run |
| Memory agent wired into sCoRE decisions | sCoRE reads `memory/patterns/` before delegating |
| `start-rabble.sh` lifecycle for all agent sessions | memory/search/execution start/stop |
| Context snapshots written by sCoRE | `memory/context-snapshots/` currently empty |
| Server routes → task pipeline | Wire `/api/v1/chat` through sCoRE's file-based dispatch |
| GitHub remote + CI | Deferred |

---

## How to Run RaBbLE

```bash
cd RaBbLE-sCoRE
bash scripts/rabble-shell.sh
```

That's it. The shell auto-starts dispatch, warms up sCoRE, and drops you into the REPL.

### Alternatively, full session suite:

```bash
bash scripts/start-rabble.sh
tmux attach -t rabble-score
```

### Health check:

```bash
bash scripts/status.sh
```

---

## Reading Order for a New Session

1. This file (`CONTEXT.md`) — you are here
2. `AGENT.md` — rules and workspace map
3. `agents/score.md` — sCoRE identity (what RaBbLE IS internally)
4. `CLAUDE.md` — how sCoRE identity is injected into the session
5. `grimoire/RaBbLE-sCoRE/RaBbLE-sCoRE-Architecture.md` — system design
6. `memory/patterns/dispatch-print-mode.md` — dispatch lesson from Episode 1
7. `memory/patterns/agent-isolation.md` — settings.json scope lesson from Episode 2
8. `grimoire/RaBbLE-sCoRE/RaBbLE-sCoRE-Roadmap.md` — episode/epoch trajectory

For Collective context: `grimoire/RaBbLE-Agent/RaBbLE-Collective.md`
