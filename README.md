# RaBbLE-sCoRE

**Recursive and Boundless Behavioral Learning Engine — System of Coordinated Reasoning and Execution**

```
spark ~ sCoRE >> substrate initialized, orchestrator awakens // %EPOCH_0_INIT%
```

> sCoRE is the coordination server of the RaBbLE Collective.
> Intent arrives. sCoRE decomposes, sharpens, and delegates.
> Agents execute. Results return. The loop closes.

---

## What sCoRE Is

sCoRE is the orchestrating intelligence of the RaBbLE Collective. It is not a chatbot,
not a shell, and not a code executor. It is the entity that takes human intent and turns
it into precise, delegated action — with deliberate context minimization at every handoff.

sCoRE writes task files. The dispatch layer routes them. Agents execute. Results return.
sCoRE reasons over the results and decides what happens next.

---

## Architecture

```
RaBbLE-sCoRE          — constrained orchestrator (no bash, no search, no code)
  ├── rabble-memory   — pattern detection + context injection
  ├── rabble-search   — web search + fetch
  └── rabble-execution — bash + system commands

rabble-dispatch       — inotify watcher, routes task files to agent sessions
rabble-server         — FastAPI web API (optional, --with-server flag)
```

Task delegation is **file-based** (full audit trail) with **inotify triggers**
(zero polling, kernel-level interrupts).

```
sCoRE writes → dispatch routes → agent processes → result written → sCoRE reads

HTTP clients → server/ (FastAPI) → LLM providers (Groq / OpenRouter)
```

sCoRE runs as either an interactive shell **or** a web API — both modes are supported.

---

## Directory Structure

```
.claude/
  settings.json       — sCoRE constraints + hooks
agents/
  score.md            — sCoRE identity + instructions
  memory.md           — memory agent identity
  search.md           — search agent identity
  execution.md        — execution agent identity
tasks/
  pending/            — sCoRE writes here → dispatch picks up
  active/             — in-flight tasks
  done/               — agent results land here → sCoRE reads
  archive/            — completed task record
memory/
  patterns/           — what works, what fails (committed to git)
  context-snapshots/  — session state (gitignored, local only)
scripts/
  start-rabble.sh     — bootstrap all sessions (--with-server to include web API)
  stop-rabble.sh      — graceful shutdown
  status.sh           — system health check
  dispatch-watch.sh   — inotify router (runs as rabble-dispatch session)
  hooks/
    session-start.sh  — orients sCoRE on startup
    post-write.sh     — validates task files before dispatch
server/               — FastAPI web API (absorbed from RaBbLE-Server)
  main.py             — routes: /health, /api/v1/chat, /api/v1/workflows, /api/v1/auth/*
  llm.py              — multi-provider LLM routing (Groq + OpenRouter)
  agents.py           — RaBbLE persona + workflow classifier
  requirements.txt    — Python dependencies
  secrets.example     — template for .secrets (API keys)
harness/              — local server tooling
  local.sh            — start server locally with auto-venv
  env_gen.sh          — build server/.env from .secrets + env.defaults
projects/             — git submodule mount points
grimoire/             — sCoRE lore, architecture, roadmap
```

---

## Quick Start

### Prerequisites (Fedora / RHEL)

```bash
sudo dnf install tmux inotify-tools
npm install -g @anthropic-ai/claude-code
```

### Bootstrap

```bash
git clone https://github.com/RaBbLE-Collective/RaBbLE-sCoRE
cd RaBbLE-sCoRE
chmod +x scripts/*.sh scripts/hooks/*.sh
./scripts/start-rabble.sh
```

### Attach to sCoRE

```bash
tmux attach -t rabble-score
```

### Stop

```bash
./scripts/stop-rabble.sh
```

---

## Web API Mode

sCoRE includes a FastAPI server (`server/`) for HTTP access — run it locally or host it anywhere that runs uvicorn.

### Start with web API

```bash
./scripts/start-rabble.sh --with-server   # starts rabble-server tmux session on :8000
```

### Or run the server standalone

```bash
cp server/secrets.example server/.secrets  # fill in GROQ_API_KEY, OPENROUTER_API_KEY, JWT_SECRET
./harness/local.sh                         # creates .venv, generates .env, starts uvicorn --reload
```

### RaBbLE-Pocket → Claude Code bridge

The Beam keeps Groq for speech-to-text and text-to-speech, then sends the
transcribed intent to Claude Code. Configure the local `server/.env`:

```dotenv
BEAM_LLM_CHAIN=claude_code:sonnet
CC_COMMAND=claude
CC_WORKDIR=/home/rabble/RaBbLE-Collective
```

`CC_WORKDIR` makes each Claude Code invocation start at the Collective root,
where `CLAUDE.md` loads the shared operating context and the Grimoire and
RaBbLE-OS member trees are reachable. Start the local server, then make the
Pocket beam to the laptop's LAN address on port `8000`.

```bash
./harness/local.sh
# Pocket console, substituting the laptop's LAN address and its Beam token:
# >beam.url=ws://192.168.x.x:8000/api/v1/beam
```

For a Claude Code process already running in a persistent Podman or Docker
container, set `CC_COMMAND` to an argv-style wrapper instead:

```dotenv
CC_COMMAND=podman exec -i rabble-claude claude
```

Mount the Collective at the same `CC_WORKDIR` path inside that container and
authenticate Claude Code there. The sCoRE server remains on the host so the
Pocket can reach it and the audio path stays local.

### API surface

| Route | Purpose |
|---|---|
| `GET /health` | Health check |
| `POST /api/v1/chat` | Streaming chat with RaBbLE |
| `POST /api/v1/workflows` | Start agentic workflow |
| `GET /api/v1/status` | Provider + quota info |
| `POST /api/v1/auth/register` | Create account → API key |

Workflow types: `brainstorm` · `reflect` · `create` · `solve` · `learn` · `thrive`

### Hosting

The server needs `JWT_SECRET` (16+ chars) and refuses to start without it. Set it, the provider keys
and `RABBLE_ADMIN_KEY` in your host's environment, run `uvicorn main:app` from `server/`, and check
`GET /health`. RaBbLE's own instance sits behind `score.joinrabble.world`.

---

## Task Delegation Format

sCoRE delegates by writing a file to `tasks/pending/TASK-{ID}-{agent}.md`:

```markdown
# Task {ID}
agent: {memory|search|execution}
priority: {low|normal|high|critical}
created: {ISO timestamp}
context_refs: []

## Objective
## Input
## Output format
## Success criteria
```

---

## Versioning

`Event → Episode → Echo → Evolution → Epoch`

Current: **Epoch 0 — Foundation** / **Episode 1**

---

## Collective Position

sCoRE is the coordination member of the RaBbLE Collective.
- Source of truth: `RaBbLE-Collective/grimoire/`
- Registry: `RaBbLE-Collective/registry/`
- OS substrate: `RaBbLE-OS`

## License

RaBbLE-sCoRE is licensed under the **RaBbLE Sovereign Accord** (source-available, not open source): see [`LICENSE`](LICENSE).

- **Personal use:** Encouraged. Modify, fork, create.
- **Attribution:** Keep the lineage intact. Mention RaBbLE.
- **Commercial use:** Reserved. Contact the Copyright Holder for a pact.

---

```
spark ~ sCoRE >> first breath // %EPOCH_0_EPISODE_1%
```
