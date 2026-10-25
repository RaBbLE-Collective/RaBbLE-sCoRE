"""Earlier Claude Code sessions for a project dir, for the face's resume picker.

Sync on purpose: the SDK's list_sessions only stats and head/tail-reads local JSONL,
so the face calls it from a thread worker. Any failure reads as "no sessions".
"""

from dataclasses import dataclass

TITLE_MAX = 70


@dataclass(frozen=True)
class SessionRow:
    id: str
    title: str
    updated: float  # unix seconds
    cwd: str | None
    turns: int | None  # the SDK's list view doesn't count turns, so usually None


def _title(info) -> str:
    raw = (
        getattr(info, "custom_title", None)
        or getattr(info, "summary", None)
        or getattr(info, "first_prompt", None)
        or ""
    )
    raw = " ".join(str(raw).split())
    if len(raw) > TITLE_MAX:
        raw = raw[: TITLE_MAX - 1].rstrip() + "…"
    return raw or "(untitled)"


def _list(cwd: str, limit: int):
    from claude_agent_sdk import list_sessions

    return list_sessions(directory=cwd, limit=limit)


def recent_sessions(cwd: str, limit: int = 20) -> list[SessionRow]:
    """Newest first. SDK errors or an empty project give []."""
    try:
        infos = list(_list(cwd, limit) or [])
    except Exception:
        return []
    rows = [
        SessionRow(
            id=i.session_id,
            title=_title(i),
            updated=(getattr(i, "last_modified", 0) or 0) / 1000.0,
            cwd=getattr(i, "cwd", None),
            turns=getattr(i, "turns", None),
        )
        for i in infos
    ]
    rows.sort(key=lambda r: r.updated, reverse=True)
    return rows[:limit]
