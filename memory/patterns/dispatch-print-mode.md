# Pattern: dispatch-print-mode

```
transcribe ~ memory >> dispatch routing pattern crystallized // %EPISODE_1%
```

## Pattern

Use `claude --dangerously-skip-permissions -p < prompt_file` for automated dispatch.
Interactive mode (`claude --dangerously-skip-permissions`) fails in tmux automation
because the TUI trust prompt and bypass-permissions warning require interactive input
that `send-keys` cannot reliably deliver before Claude's startup timing.

## What failed

- `tmux new-session` + `sleep 0.5` + `tmux send-keys` — too fragile
  - Trust dialog appears, absorbs keystrokes meant for the task prompt
  - Bypass-permissions warning navigation requires arrow keys, not numeric input
  - Session exits without writing any result

## What works

```bash
prompt_file=$(mktemp)
cat > "$prompt_file" << PROMPT
$(cat agents/$agent.md)
---
Task instructions...
PROMPT

tmux new-session -d -s "$session" -c "$REPO_ROOT" \
  "claude --dangerously-skip-permissions -p < '$prompt_file' ; rm -f '$prompt_file'"
```

- Print mode (`-p`) skips TUI entirely — no trust prompt, no bypass warning
- Agent identity injected at top of prompt via temp file
- Session exits cleanly when claude finishes
- Task file + result file lifecycle confirmed working end-to-end

## First confirmed

Episode 1 — TASK-0001-search. Full round-trip: pending → active → done → archive.
