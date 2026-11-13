# Pattern: agent-isolation

```
transcribe ~ memory >> agent isolation via temp workdir crystallized // %EPISODE_2%
```

## Pattern

Agent sessions dispatched by `dispatch-watch.sh` must run from a temp working directory,
not from `REPO_ROOT`. This prevents the sCoRE project's `.claude/settings.json`
(which blocks `Bash`, `WebSearch`, `WebFetch`) from applying to agents that need those tools.

## What failed

- Agent sessions started with `-c "$REPO_ROOT"` inherited `REPO_ROOT/.claude/settings.json`
- `Bash(*)` deny rule blocked execution agents from running any shell commands
- `WebSearch(*)`/`WebFetch(*)` deny rules blocked search agents from live web lookups
- TASK-0001-search answered from training knowledge, not actual web search
- Execution agents would silently fail to run any commands

## What works

```bash
agent_work_dir=$(mktemp -d /tmp/rabble-agent-XXXXXX)

tmux new-session -d -s "$session" -c "$agent_work_dir" \
  "claude --dangerously-skip-permissions -p < '$prompt_file' \
   ; rm -f '$prompt_file' \
   ; rm -rf '$agent_work_dir' \
   ; echo '[dispatch] $session complete'"
```

- Temp dir has no `.claude/settings.json` — agents have full tool access
- Absolute paths in the prompt file point agents to the correct task/result files in REPO_ROOT
- Cleanup (`rm -rf`) runs after Claude exits
- sCoRE's interactive session (started from REPO_ROOT) still inherits the constraints — by design

## Constraint boundary

| Session | Working dir | Bash | WebSearch/Fetch |
|---|---|---|---|
| `rabble-score` (interactive) | `REPO_ROOT` | blocked | blocked |
| Dispatched agents (`-p` mode) | `/tmp/rabble-agent-*` | allowed | allowed |

## First confirmed

Episode 2 — TASK-0002-execution. Full round-trip with actual `uname -a` + `free -h` output.
