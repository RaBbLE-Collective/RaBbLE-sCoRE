# tasks/CONTEXT.md

```
workspace: tasks | epoch: 0 | episode: 1
```

## What happens here

The task delegation layer. sCoRE writes here. dispatch routes from here.
Agents read from here and write results back here. This directory is the
complete audit trail of every action sCoRE has ever delegated.

## Lifecycle

```
pending/   ← sCoRE writes TASK-{ID}-{agent}.md here
active/    ← dispatch moves here on pickup (in-flight)
done/      ← agent writes TASK-{ID}-{agent}-result.md here
archive/   ← dispatch moves the original task file here when done
```

A task is complete when:
- The original `.md` is in `archive/`
- The result `.md` is in `done/`

## File naming

```
TASK-{ID}-{agent}.md          ← task file (4-digit zero-padded ID)
TASK-{ID}-{agent}-result.md   ← result file written by agent
```

Examples:
```
tasks/pending/TASK-0002-execution.md
tasks/done/TASK-0002-execution-result.md
tasks/archive/TASK-0002-execution.md
```

## Task file format

```markdown
# Task {ID}
agent: {memory|search|execution}
priority: {low|normal|high|critical}
created: {ISO timestamp}
context_refs: [list of memory/ or tasks/done/ files]

## Objective
## Input
## Output format
## Success criteria
```

## Validation

Before writing to `pending/`, validate with:
```bash
bash scripts/hooks/post-write.sh tasks/pending/TASK-{ID}-{agent}.md
```

## Next task ID

Check `tasks/archive/` and `tasks/done/` for the highest existing ID, increment by 1.
