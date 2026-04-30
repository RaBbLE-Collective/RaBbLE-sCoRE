# .claude/CONTEXT.md

```
workspace: .claude | epoch: 0 | episode: 1
```

## What happens here

Claude Code configuration for the sCoRE session. This is the constraint layer —
it enforces sCoRE's identity at the tool level, not just by instruction.

## Contents

| File | Purpose |
|---|---|
| `settings.json` | Permission denials (Bash, WebSearch, WebFetch blocked) + hooks |

## Constraint design

sCoRE is the orchestrator. It must not execute code, run shell commands, or search
the web directly. These capabilities are intentionally blocked here so that the
*architecture enforces the constraint* — not just the system prompt.

When sCoRE feels the urge to run bash or search the web, the tool call is blocked
at the settings level. This forces delegation, which is the correct behavior.

```json
"deny": [
  "Bash(*)",
  "WebSearch(*)",
  "WebFetch(*)"
]
```

## Agent sessions do NOT use this settings.json

Each agent (`rabble-search`, `rabble-execution`, etc.) runs in its own tmux session
with `claude -p`. They do not inherit `.claude/settings.json` from this directory
unless they are launched from within it. Agent constraints are defined by their
identity files in `agents/`.

## Modifying constraints

If a capability needs to be added to sCoRE itself (not delegated), consider carefully
whether that breaks the orchestrator-only model. If yes, create a new agent instead.
