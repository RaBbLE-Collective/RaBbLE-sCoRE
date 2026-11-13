# memory/CONTEXT.md

```
workspace: memory | epoch: 0 | episode: 1
```

## What happens here

The sCoRE memory layer. Two distinct stores with different purposes and commit policies.

## Contents

| Directory | Purpose | Git policy |
|---|---|---|
| `patterns/` | What works, what fails. Reusable delegation intelligence. | Committed — shared across sessions |
| `context-snapshots/` | Session state summaries. Prior session awareness. | Gitignored — local only |

## patterns/

Committed knowledge. Each file is a discrete pattern — a named lesson about how
the system behaves. sCoRE should read these before starting any complex task.

Format for a pattern file:
```markdown
# Pattern: {name}
## Pattern
[the rule or observation]
## When it bites
[failure conditions]
## Mitigation
[what to do instead]
## First observed
[episode + context]
```

Current patterns:
- `dispatch-routing.md` — task file must have valid `agent:` field or dispatch silently drops it
- `dispatch-print-mode.md` — use `claude -p < prompt_file`, not interactive + send-keys

## context-snapshots/

Local only. Written by `scripts/hooks/session-start.sh --summarize` at session end.
sCoRE reads the most recent snapshot on startup to resume with prior context.

Format: `snapshot-{ISO-date}.md`

## Writing a new pattern

After any significant lesson (something worked unexpectedly, something failed):
1. Write `memory/patterns/{short-name}.md`
2. Commit it — patterns are collective knowledge, not session state
3. Reference it in the relevant task result if applicable
