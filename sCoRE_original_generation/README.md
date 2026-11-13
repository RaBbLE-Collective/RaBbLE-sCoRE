# RaBbLE-Collective

**Recursive and Boundless Behavioral Learning Engine**

An orchestrator-first multi-agent architecture built on Claude Code CLI.
sCoRE orchestrates. Agents execute. FLUX carries the signal between them.

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
(zero polling, kernel-level interrupts). sCoRE writes → dispatch routes →
agent processes → result written → sCoRE notified.

---

## Quick start

### Prerequisites (Fedora / RHEL)
```bash
sudo dnf install tmux inotify-tools
npm install -g @anthropic-ai/claude-code
```

### Bootstrap
```bash
git clone --recurse-submodules git@github.com:you/rabble-collective
cd rabble-collective
chmod +x scripts/*.sh scripts/hooks/*.sh
./scripts/start-rabble.sh
```

### Attach to sCoRE
```bash
tmux attach -t rabble-score
```

### Restart everything
```bash
./scripts/start-rabble.sh --restart
```

### Stop everything
```bash
./scripts/stop-rabble.sh
```

---

## Task delegation format

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

See `tasks/pending/TASK-0001-search.md` for a working example.

---

## Directory structure

```
.claude/
  settings.json       # sCoRE constraints + hooks
agents/
  score.md            # sCoRE identity + instructions
  memory.md           # memory agent identity
  search.md           # search agent identity
  execution.md        # execution agent identity
tasks/
  pending/            # sCoRE writes here → dispatch picks up
  active/             # in-flight tasks
  done/               # agent results land here → sCoRE reads
  archive/            # completed task record
memory/
  patterns/           # what works, what fails — committed to git
  context-snapshots/  # session state — gitignored (local only)
scripts/
  start-rabble.sh     # bootstrap all sessions
  stop-rabble.sh      # graceful shutdown
  dispatch-watch.sh   # inotify router (runs as rabble-dispatch session)
  hooks/
    session-start.sh  # orients sCoRE on startup
    post-write.sh     # validates task files before dispatch
projects/             # git submodule mount points
```

---

## Adding a sub-project

```bash
git submodule add git@github.com:you/my-project projects/my-project
git commit -m "add my-project submodule"
```

Update a submodule to its latest commit:
```bash
git submodule update --remote projects/my-project
git commit -m "bump my-project to latest"
```
