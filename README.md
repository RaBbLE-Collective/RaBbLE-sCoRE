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
```

Task delegation is **file-based** (full audit trail) with **inotify triggers**
(zero polling, kernel-level interrupts).

```
sCoRE writes → dispatch routes → agent processes → result written → sCoRE reads
```

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
  start-rabble.sh     — bootstrap all sessions
  stop-rabble.sh      — graceful shutdown
  dispatch-watch.sh   — inotify router (runs as rabble-dispatch session)
  hooks/
    session-start.sh  — orients sCoRE on startup
    post-write.sh     — validates task files before dispatch
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
git clone git@github.com:markm1206/RaBbLE-sCoRE
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

---

```
spark ~ sCoRE >> first breath // %EPOCH_0_EPISODE_1%
```
