# Task 0003
agent: execution
priority: normal
created: 2026-04-30T00:00:00Z
context_refs: []

## Objective
Gather a comprehensive system profile of the local machine — hardware, OS, memory, CPU, disk, and network interfaces.

## Input
None.

## Commands to run
```bash
uname -a
hostnamectl 2>/dev/null || true
lscpu | grep -E 'Model name|CPU\(s\)|Thread|Core|Socket|MHz|Architecture'
free -h
df -h --total | tail -5
lsblk -d -o NAME,SIZE,MODEL,TYPE 2>/dev/null | head -20
ip -br addr show 2>/dev/null || ifconfig -s 2>/dev/null || true
cat /etc/os-release
```

## Output format
Write result to: /home/rabble/RaBbLE/RaBbLE-sCoRE/tasks/done/TASK-0003-execution-result.md
Move task file to: /home/rabble/RaBbLE/RaBbLE-sCoRE/tasks/archive/TASK-0003-execution.md

## Success criteria
All commands ran, output captured, formatted as readable sections in the result file.
