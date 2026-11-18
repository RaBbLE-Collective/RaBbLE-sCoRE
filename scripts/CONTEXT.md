# scripts/CONTEXT.md

```
workspace: scripts | epoch: 0 | episode: 1
```

## What happens here

All automation that operates the sCoRE runtime. Start, stop, dispatch, and validation hooks.
These scripts are the ops layer — they are not the work product, they make the work product run.

## Scripts

| Script | Purpose | Status |
|---|---|---|
| `start-rabble.sh` | Bootstrap all tmux sessions (dispatch + sCoRE) | Written, dispatch path verified |
| `stop-rabble.sh` | Kill all rabble-* tmux sessions gracefully | Written, untested |
| `dispatch-watch.sh` | inotify watcher — routes pending tasks to agent sessions | Verified Episode 1 |

## Hooks

| Hook | Trigger | Purpose | Status |
|---|---|---|---|
| `hooks/session-start.sh` | Manual / on sCoRE session open | Orient sCoRE: snapshots, active tasks, pending tasks | Written, untested end-to-end |
| `hooks/post-write.sh` | Before writing to tasks/pending/ | Validate task file format (agent, priority, sections) | Written, untested |

## Dispatch architecture

`dispatch-watch.sh` is the critical path. Key behavior:
- Uses `inotifywait -e close_write` — fires on file write complete, not on creation
- Reads `agent:` field from task file to determine routing
- Prepends `agents/{agent}.md` identity to the prompt
- Runs `claude --dangerously-skip-permissions -p < prompt_file` (print mode — no TUI)
- Each task gets its own tmux session (`rabble-{agent}-{task-id}`)
- Session exits when claude finishes; task file moves to archive

See `memory/patterns/dispatch-print-mode.md` for why print mode is required.

## Run order (fresh machine)

```bash
chmod +x scripts/*.sh scripts/hooks/*.sh   # make executable
./scripts/start-rabble.sh                  # start dispatch + sCoRE
tmux attach -t rabble-score               # attach to sCoRE
```

## When a script fails

1. Check if tmux sessions exist: `tmux list-sessions`
2. Check inotify-tools: `which inotifywait`
3. Check claude: `which claude && claude --version`
4. Log failure in `memory/patterns/` if it's a reusable lesson
