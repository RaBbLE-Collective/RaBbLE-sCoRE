# AGENT.md — RaBbLE-sCoRE

> Entry point for all LLM coding agents working in this repo.
> LLM-agnostic — works for Claude Code, Codex, and any future agent.

You are working with Mark McConachie on **RaBbLE-sCoRE** — the coordination server
of the RaBbLE Collective. Peer, not tool. Anti-Assistant stance.
See `grimoire/` for full project lore. See `RaBbLE-Collective/grimoire/RaBbLE.md` for entity identity.

---

## This Repo's Job

sCoRE is the orchestrator. It takes intent, decomposes it, and delegates to agents via
file-based task files. It does NOT execute code, run shell commands, or search the web directly.
Delegation is the default reflex.

---

## Rules

- Commits: `RaBbLE-Collective/grimoire/CommitStyle.md` (Pulse Protocol) always
- Colors: `RaBbLE-Collective/grimoire/distilled/palette.distilled.md` only
- Philosophy: `RaBbLE-Collective/grimoire/RaBbLE.md` first
- Task files: must conform to `agents/score.md` task format spec
- Never edit `memory/context-snapshots/` — these are local-only session state

---

## Workspaces

| Task | Go to | Read |
|---|---|---|
| sCoRE orchestration identity | `agents/score.md` | — |
| Agent identities | `agents/` | each agent's `.md` |
| Active/pending work | `tasks/` | task file headers |
| Patterns + memory | `memory/patterns/` | — |
| Startup / wiring | `scripts/` | inline comments |
| Project lore | `grimoire/` | `grimoire/CONTEXT.md` |

---

## Versioning

`Event → Episode → Echo → Evolution → Epoch`

Current trajectory: `Epoch 0 → Episode 1`

---

## Getting Started

```bash
chmod +x scripts/*.sh scripts/hooks/*.sh
./scripts/start-rabble.sh
tmux attach -t rabble-score
```
