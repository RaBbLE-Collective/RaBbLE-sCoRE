# RaBbLE-sCoRE-Roadmap.md

```
transcribe ~ grimoire >> sCoRE trajectory mapped // %TRAJECTORY_LOCKED%
```

## Versioning Model

`Event → Episode → Echo → Evolution → Epoch`

- **Event** — a commit
- **Episode** — everyday development, a collection of events
- **Echo** — stable release state
- **Evolution** — significant architectural change
- **Epoch** — broadest era

---

## Epoch Map

```
Epoch 0: FOUNDATION     [ACTIVE]  — Scaffold, identity, dispatch loop wired
Epoch 1: FIRST LOOP     [FUTURE]  — sCoRE ↔ agent round-trip working end-to-end
Epoch 2: LEARNING       [FUTURE]  — Memory patterns inform delegation decisions
```

---

## Current Epoch — Epoch 0: Foundation

**Intent:** Get the substrate in place. Scaffold committed. Dispatch wired.
sCoRE can receive intent and write valid task files. One agent (execution) can receive
a task and return a result. The loop is manually verified.

**Exit Condition:**
- Repo initialized, scaffold committed to `main`
- `.claude/settings.json` constraints verified (Bash blocked in sCoRE session)
- `dispatch-watch.sh` wiring tested with a synthetic task file
- At least one real round-trip: sCoRE → task file → execution agent → result

---

## Episode Tracker

### Episode 1 — First Breath `[COMPLETE]`

**Branch:** `episode-1` | **Tag:** `echo-1.0`
**Goal:** Scaffold committed. Dispatch script wired. sCoRE session launchable.

Events:
- [x] Repo initialized with scaffold
- [x] `.claude/settings.json` constraints in place (Bash/Web blocked at tool level)
- [x] `dispatch-watch.sh` round-trip verified — TASK-0001-search full lifecycle
- [x] Learned: dispatch must use `claude -p < prompt_file`, not interactive + send-keys
- [x] `start-rabble.sh` verified — dispatch + sCoRE session both launch correctly
- [x] CONTEXT.md handoff layer added to all work areas
- [x] Episode 1 Echo tagged `echo-1.0`

---

## Revision History

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-04-29 | Initial roadmap — Episode 1 scaffold |

---

```
transcribe ~ grimoire >> trajectory crystallized // %TRAJECTORY_LOCKED%
```
