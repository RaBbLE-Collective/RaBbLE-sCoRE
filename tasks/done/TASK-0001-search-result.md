# Task 0001 — Result
agent: search
completed: 2026-04-29T00:00:00Z

## Sources
- [inotify-tools dnf info](https://github.com/inotify-tools/inotify-tools) — verified via live `dnf info inotify-tools` query on Fedora 43 x86_64
- Verified locally: `which inotifywait` → `/usr/bin/inotifywait`

## Findings

## inotify-tools on Fedora 43
- Package name: `inotify-tools`
- Install: `sudo dnf install inotify-tools`
- inotifywait binary path after install: `/usr/bin/inotifywait`
- Version available: `4.23.9.0-5.fc43` (from the base `fedora` repo — no third-party repo required)

Additional notes:
- Package is in the default Fedora 43 base repository (`fedora`), so no extra repo configuration is needed.
- Also provides `inotifywatch` at `/usr/bin/inotifywatch`.
- `inotifywait` does not support `--version`; version is only visible via `dnf info`.

## Gaps / Uncertainty
- None. Package confirmed present in the base Fedora 43 repo and already installed on the test machine. Install command is copy-pasteable and correct.
