# RaBbLE-sCoRE-Architecture.md

```
transcribe ~ grimoire >> sCoRE architecture crystallized // %EPOCH_0_ARCH%
```

## System Role

sCoRE is the coordination server of the RaBbLE Collective.
It is the single point where human intent enters the system and is decomposed into
delegated actions. It has no execution surface of its own — delegation is its only output.

---

## Component Map

```
┌─────────────────────────────────────────────────────────┐
│                     HUMAN INTENT                        │
│              (natural language, direct session)         │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                   RaBbLE-sCoRE                          │
│  Claude Code session — agents/score.md identity         │
│  Reads: memory/, tasks/done/, tasks/active/             │
│  Writes: tasks/pending/                                 │
│  Cannot: Bash, WebSearch, WebFetch                      │
└───────────────────────┬─────────────────────────────────┘
                        │ writes TASK-{ID}-{agent}.md
                        ▼
┌─────────────────────────────────────────────────────────┐
│                  rabble-dispatch                        │
│  inotifywait on tasks/pending/                         │
│  Routes task files → agent tmux sessions               │
│  Moves: pending/ → active/ on pickup                   │
└──────────┬──────────────┬──────────────┬───────────────┘
           │              │              │
           ▼              ▼              ▼
    rabble-memory  rabble-search  rabble-execution
    (patterns,     (web search,   (bash, system
     context)       fetch)         commands)
           │              │              │
           └──────────────┴──────────────┘
                          │
                          ▼ writes TASK-{ID}-{agent}-result.md
                    tasks/done/
                          │
                          ▼
                    sCoRE reads → reasons → next task or done
```

---

## Task Lifecycle

```
1. sCoRE writes   → tasks/pending/TASK-{ID}-{agent}.md
2. dispatch picks → moves to tasks/active/, routes to agent session
3. Agent executes → writes result to tasks/done/
4. dispatch moves → tasks/active/ → tasks/archive/
5. sCoRE reads    → tasks/done/ result → continues reasoning
```

---

## Key Design Decisions

**File-based delegation over API calls**
Every task delegation is a file write. This gives a full audit trail, survives session
restarts, and makes the system debuggable without instrumentation.

**inotify over polling**
Kernel-level interrupt for zero CPU overhead on idle. No sleep loops. No missed events.

**Agent isolation via CLAUDE.md constraints**
Each agent session runs Claude Code with its own identity file. sCoRE's settings.json
blocks Bash/Web at the tool level — not just by instruction.

**Context minimization at every handoff**
Task files include only what the agent needs (via context_refs). Agents have no implicit
memory of prior tasks — they get exactly what sCoRE provides, nothing more.

---

## Session Architecture (tmux)

| Session | Identity | Constraints |
|---|---|---|
| `rabble-score` | `agents/score.md` | No Bash, No Web |
| `rabble-dispatch` | shell script | No Claude — pure bash |
| `rabble-memory` | `agents/memory.md` | Read/write workspace only |
| `rabble-search` | `agents/search.md` | Web access permitted |
| `rabble-execution` | `agents/execution.md` | Full Bash access |

---

## Revision History

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-04-29 | Initial architecture document — Episode 1 scaffold |

---

```
transcribe ~ grimoire >> architecture mapped // %EPOCH_0_EPISODE_1%
```
