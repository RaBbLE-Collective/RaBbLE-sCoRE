# sCoRE System Prompt Setup

**Goal:** Load `system-prompt-sCoRE.md` when Claude Code runs *as* RaBbLE-sCoRE (entity session), but NOT when working *on* sCoRE code (project work).

## Implementation

Add to `.claude/settings.json` (in sCoRE root):

```json
{
  "system_prompt_file": "system-prompt-sCoRE.md"
}
```

Or use Claude Code UI: `/config system_prompt_file system-prompt-sCoRE.md`

### When This Applies

- **Load:** Claude Code running as RaBbLE-sCoRE (via `scripts/start-rabble.sh`)
- **Do NOT load:** Claude Code doing project work on sCoRE code (normal sessions)

### Verification

After setting, start a sCoRE session:
```bash
cd RaBbLE-sCoRE
bash scripts/start-rabble.sh
```

sCoRE should greet with its delegation model (task file workflow), not project onboarding.

## Files

- **System prompt:** `system-prompt-sCoRE.md` (sCoRE's internal constraints)
- **Project AGENT.md:** `AGENT.md` (for agents working on sCoRE code)
- **sCoRE agent identity:** `agents/score.md` (included by system-prompt-sCoRE.md)

## Related

- `.claude/settings.json` — project-level config (sCoRE root)
- `AGENT.md` — entry point for project work
- `CONTEXT.md` — episode tracker and status
