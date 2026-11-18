# Pattern: dispatch-routing

```
transcribe ~ memory >> first pattern logged // %PATTERN_INIT%
```

## Pattern

Task files must have an `agent:` field on line 2 (after the title).
`dispatch-watch.sh` uses `grep -m1 '^agent:'` — if the field is missing or malformed,
the task is silently skipped.

## When it bites

- Task file written without `agent:` field → dispatch silently drops it
- Agent value typo (`excecution` instead of `execution`) → dispatch drops it
- Valid agents: `memory`, `search`, `execution` — nothing else

## Mitigation

Run `scripts/hooks/post-write.sh {task-file}` before writing to `tasks/pending/`.
This validates all required fields and sections before dispatch sees the file.

## First observed

Episode 1 — design review during scaffold phase. Pre-emptive pattern, not post-incident.
