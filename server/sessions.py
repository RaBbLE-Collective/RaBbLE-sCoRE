import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

import users as user_store


class SessionMessage(BaseModel):
    role: str
    content: str
    timestamp: str


class SessionRecord(BaseModel):
    session_id: str
    user_id: str
    created_at: str
    last_active: str
    title: Optional[str] = None
    messages: list[SessionMessage] = []


def _sessions_dir(user_id: str) -> Path:
    base = user_store.get_data_dir() / "sessions" / user_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _session_path(user_id: str, session_id: str) -> Path:
    return _sessions_dir(user_id) / f"{session_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auto_title(messages: list[SessionMessage]) -> str:
    for m in messages:
        if m.role == "user":
            words = m.content.split()[:8]
            return " ".join(words) + ("…" if len(m.content.split()) > 8 else "")
    return "Untitled session"


def create_session(user_id: str) -> SessionRecord:
    now = _now()
    record = SessionRecord(
        session_id=secrets.token_urlsafe(16),
        user_id=user_id,
        created_at=now,
        last_active=now,
    )
    _session_path(user_id, record.session_id).write_text(record.model_dump_json())
    return record


def get_session(session_id: str, user_id: str) -> Optional[SessionRecord]:
    path = _session_path(user_id, session_id)
    if not path.exists():
        return None
    return SessionRecord.model_validate_json(path.read_text())


def list_user_sessions(user_id: str) -> list[SessionRecord]:
    sessions_dir = _sessions_dir(user_id)
    result = []
    for f in sessions_dir.glob("*.json"):
        try:
            rec = SessionRecord.model_validate_json(f.read_text())
            # Strip messages for listing
            result.append(rec.model_copy(update={"messages": []}))
        except Exception:
            pass
    result.sort(key=lambda r: r.last_active, reverse=True)
    return result


def append_message(session_id: str, user_id: str, role: str, content: str) -> None:
    record = get_session(session_id, user_id)
    if not record:
        return
    msg = SessionMessage(role=role, content=content, timestamp=_now())
    record.messages.append(msg)
    record.last_active = msg.timestamp
    if not record.title and len(record.messages) >= 2:
        record.title = _auto_title(record.messages)
    _session_path(user_id, session_id).write_text(record.model_dump_json())


def get_or_create_default(user_id: str) -> SessionRecord:
    existing = list_user_sessions(user_id)
    if existing:
        # list_user_sessions returns newest-first
        return existing[0]
    return create_session(user_id)
