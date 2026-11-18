# Task 0002
agent: execution
priority: low
created: 2026-04-29T00:00:00Z
context_refs: []

## Objective
Run `uname -a` and `free -h` and return the output.

## Input
No input needed beyond the commands above.

## Output format
Plain output from each command, labeled. Write the result to:
`/home/rabble/RaBbLE/RaBbLE-sCoRE/tasks/done/TASK-0002-execution-result.md`

Use this format:
```
# Task 0002 — Result
agent: execution
completed: <ISO timestamp>
exit_code: 0

## Command(s) Run
uname -a
free -h

## stdout

### uname -a
<output>

### free -h
<output>

## stderr
(none)

## State Changes
None — read-only commands.

## Success
yes
```

After writing the result, move the task file to:
`/home/rabble/RaBbLE/RaBbLE-sCoRE/tasks/archive/TASK-0002-execution.md`

## Success criteria
Both commands ran, exit code 0, result file written, task file archived.
