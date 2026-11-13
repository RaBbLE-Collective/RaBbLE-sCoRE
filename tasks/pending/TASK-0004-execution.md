# Task 0004
agent: execution
priority: normal
created: 2026-04-30T00:00:00Z
context_refs: []

## Objective
Gather a concise system profile of the local machine.

## Input
None.

## Commands to run
```bash
uname -a
hostnamectl 2>/dev/null || true
lscpu | grep -E "Model name|CPU\(s\)|Thread|Core|Socket"
free -h
df -h --total | tail -1
cat /etc/os-release | grep -E "^NAME|^VERSION"
```

## Output format
Write results to: /home/rabble/RaBbLE/RaBbLE-sCoRE/tasks/done/TASK-0004-execution-result.md
Move task file to: /home/rabble/RaBbLE/RaBbLE-sCoRE/tasks/archive/TASK-0004-execution.md

## Success criteria
All commands ran, output captured, exit codes 0 (non-critical commands may fail gracefully).
