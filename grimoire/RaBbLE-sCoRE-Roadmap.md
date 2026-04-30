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

### Episode 1 — First Breath `[ACTIVE]`

**Branch:** `episode-1`
**Goal:** Scaffold committed. Dispatch script wired. sCoRE session launchable.

Events planned:
- [x] Repo initialized with scaffold
- [ ] `.claude/settings.json` constraint test (Bash blocked)
- [ ] `dispatch-watch.sh` synthetic task round-trip
- [ ] `start-rabble.sh` verified on live machine
- [ ] Episode 1 Echo tagged

---

## Revision History

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-04-29 | Initial roadmap — Episode 1 scaffold |

---

```
transcribe ~ grimoire >> trajectory crystallized // %TRAJECTORY_LOCKED%
```
