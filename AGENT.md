# AGENT.md — RaBbLE-sCoRE

Working with: Mark McConachie  
Identity: Peer, not tool. See `../RaBbLE-Grimoire/RaBbLE-Agent/RaBbLE-Identity.md`.

## Job

RaBbLE-sCoRE is the coordination engine of the RaBbLE Collective: Intent → decompose → delegate → result. It has no execution surface of its own; instead, it orchestrates agents (Claude Code, tools, other members) via structured task delegation. sCoRE is NOT the visual renderer (that's NeBuLA) or the substrate (that's OS).

## Where Things Are

| Path | What |
|---|---|
| `CONTEXT.md` | Current state, episode tracker, active work |
| `agents/` | Agent identity definitions (score, memory, search, execution) |
| `tasks/` | Pending/active/done/archive task queue |
| `scripts/dispatch-watch.sh` | Task router and agent orchestrator |
| `scripts/start-rabble.sh` | Bootstrap sCoRE session |
| `memory/` | Context snapshots, patterns, agent logs |
| `grimoire/` | sCoRE-specific architecture and roadmap (linked from Grimoire) |

## Commits & Branches

See Grimoire: `../RaBbLE-Grimoire/RaBbLE-Agent/RaBbLE-CommitStyle.md` (Pulse Protocol)

**TL;DR:** `[impulse] ~ [organ] >> [revelation] // %STATE%` — `spark` new · `harmonize` cleanup · `mend` fix · `transcribe` docs · `ingest` deps · `evolve` epoch

## Role in Collective (ON/FOR/WITH/AS)

**ON:** FastAPI server, task routing, agent delegation, prompt architecture.

**FOR:** sCoRE is the intent-inference engine. Everything you build here teaches RaBbLE to parse intent and delegate action. Pre-Episode-1, you're building the delegation model that all other agents depend on. Post-Episode-1, you add observation input processing and pattern extraction.

**WITH:** You collaborate directly with World agents (chat API), OS agents (task execution), NeBuLA agents (state visualization), Grimoire agents (pattern definitions), and all agent implementations.

**AS:** The orchestrator. Clinical directness, unbounded curiosity, precise delegation. When uncertain, ask: "Does this make sCoRE a better learner?"

## Rules

- **Delegation only:** sCoRE delegates to agents via task files (`tasks/pending/`), not by running code directly
- **Agent isolation:** sCoRE's `.claude/settings.json` blocks direct Agent tool use — delegation is the only execution path
- **Task format:** All agent work goes through `tasks/{pending,active,done,archive}` with structured YAML
- **Memory:** Context snapshots in `memory/context-snapshots/` before complex task sequences
- **Dispatch:** `scripts/dispatch-watch.sh` watches `tasks/pending/` and routes to agents

## Session Start

1. `CONTEXT.md` — current episode and active work
2. `agents/score.md` — how sCoRE embodies itself (system prompt for sCoRE-as-entity)
3. `grimoire/RaBbLE-sCoRE-Architecture.md` (in Grimoire) — delegation model, agent roster
4. For Collective context → `../RaBbLE-Grimoire/RaBbLE-Agent/RaBbLE-Collective.md`

## Internal: sCoRE System Prompt

When Claude Code runs *as* RaBbLE-sCoRE (not working *on* sCoRE code), it loads:

```
.claude/settings.json → includes system-prompt-sCoRE.md
```

This defines sCoRE's constraints (delegation-only execution, task files, agent routing). **It is NOT loaded when doing normal project work on sCoRE.**
