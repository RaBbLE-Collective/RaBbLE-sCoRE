# agents/CONTEXT.md

```
workspace: agents | epoch: 0 | episode: 1
```

## What happens here

Agent identity files. Each file is the system prompt / behavioral spec for one Claude Code
session. sCoRE is constrained (no Bash, no Web). The three sub-agents have different
capability surfaces depending on what the task requires.

## Contents

| File | Session name | Capabilities | Constraints |
|---|---|---|---|
| `score.md` | `rabble-score` | Read workspace, write tasks/memory | No Bash, no Web, no code |
| `memory.md` | `rabble-memory` | Read/write workspace | No Bash, no Web |
| `search.md` | `rabble-search` | Web search + fetch | No Bash, no local exec |
| `execution.md` | `rabble-execution` | Bash, system commands, file I/O | Full shell access |

## How these files are used

`dispatch-watch.sh` reads `agents/{agent}.md` and prepends it to the task prompt
before running `claude --dangerously-skip-permissions -p`. The agent receives its
identity + the task in a single stdin prompt.

sCoRE's own session (`rabble-score`) reads `score.md` as its CLAUDE.md equivalent.

## Adding a new agent

1. Write `agents/{name}.md` — identity, capabilities, constraints, output format
2. Add the session name to `scripts/start-rabble.sh` if it should be persistent
3. Add `{name}` to the valid agent list in `scripts/hooks/post-write.sh`
4. Document it in this file
