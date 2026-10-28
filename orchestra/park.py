"""Parking: leave a bloated session cleanly and start a fresh one from a handoff.

Resuming a long session later re-reads its whole context uncached once the prompt cache has
expired, and a full window ends in Claude Code's auto-compaction (damage that never recovers).
So at PARK_AT percent the lead, still warm in cache, writes a cold-start handoff and the session
log entry; the engine checks the file exists and starts a new session that reads it. The old
session is never resumed.

Pure: paths, prompts and the file check. The turn handling lives in ui/app.py.
"""

import time
from pathlib import Path

PARK_AT = 80.0   # % of the context window: park at the next turn boundary
WARN_AT = 70.0   # one notice, so the park never comes as a surprise
MIN_BYTES = 300  # a handoff shorter than this is a stub, not a cold start

MODES = ("auto", "ask", "off")


def handoff_dir(cwd: str | Path) -> Path:
    """The Collective's Grimoire handoff folder when cwd is inside one, else <cwd>/.rabble/handoffs."""
    here = Path(cwd).resolve()
    for d in (here, *here.parents):
        grimoire = d / "RaBbLE-Grimoire" / "log" / "handoffs"
        if grimoire.is_dir():
            return grimoire
        if (d / "log" / "handoffs").is_dir() and (d / "log" / "SESSION-LOG.md").exists():
            return d / "log" / "handoffs"
    return here / ".rabble" / "handoffs"


def handoff_path(cwd: str | Path, session_id: str | None, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(now if now is not None else time.time()))
    return handoff_dir(cwd) / f"HANDOFF-parked-{stamp}-{(session_id or 'session')[:8]}.md"


def park_prompt(path: Path, pct: float) -> str:
    return (
        f"PARK THIS SESSION. Context is {pct:.0f}% full. A fresh session will continue the work and this one "
        "will not be resumed, so the handoff must stand alone. Do these in order, tersely:\n\n"
        f"1. Write the handoff to exactly this path: {path}\n"
        "   Sections: the goal in one paragraph; state now (what works, what is half-done); what this "
        "session did (files, commits); what is next, ordered and concrete with the exact commands; "
        "open decisions waiting on Mark; things learned that the repo does not record; the files to read "
        "first. Under ~120 lines. The next session remembers nothing of this one.\n"
        "2. Add this session's entry to the session log, following the repo's own instructions (do not "
        "hand-write the LATEST box). If there is no session log here, skip this step.\n"
        "3. Commit only if the work is at a good commit point (one coherent change, checks pass). "
        "Otherwise leave it uncommitted and list the uncommitted files, and why, in the handoff. "
        "Never commit unrelated changes. Never push.\n"
        "4. Reply with one line: the handoff path. Do not start any new work."
    )


def fresh_prompt(path: Path, keep_going: bool) -> str:
    base = (f"This is a fresh session parked from an earlier one that ran out of room. Read {path} in full, "
            "then the files it says to read first. ")
    if keep_going:
        return base + ("Then continue with its 'next' list from the first item. Stop and ask at "
                       "anything that needs my decision.")
    return base + "Then say in three lines where things stand and what you would do next, and wait for me."


def check_handoff(path: Path, since: float) -> str | None:
    """None when the handoff is there and real; otherwise why not."""
    try:
        st = path.stat()
    except OSError:
        return f"no file at {path}"
    if st.st_mtime < since - 1:
        return f"{path.name} was not written this turn"
    if st.st_size < MIN_BYTES:
        return f"{path.name} is only {st.st_size} bytes"
    return None
