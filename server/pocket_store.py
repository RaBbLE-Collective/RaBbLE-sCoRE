"""
Pocket data kept per member account: captures (notes) and conversations. The HTTP surface is
pocket_routes.py; beam.py writes here. Contract: Grimoire RaBbLE-Pocket/RaBbLE-Pocket-Beam-Protocol.md.

Only real member accounts are kept. Not guests (tier guest, or the shared "guest" id), not the shared
BEAM_DEVICE_TOKEN user "pocket", not entity accounts, not the shared admin key. Every query is scoped
by user_id, so another member's id is simply not found. beam.py wraps writes so a store failure
never breaks a turn or a capture.
"""

import re
import secrets
from datetime import datetime, timezone

import db

PREVIEW = 160
_SHARED = {"pocket", "guest", "admin", ""}
_NOT_KEPT_TIERS = {"guest", "entity"}


def persists(user: dict) -> bool:
    return user.get("sub") not in _SHARED and user.get("tier") not in _NOT_KEPT_TIERS


def iso_from_epoch(secs: float) -> str:
    return datetime.fromtimestamp(secs, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def norm_ts(value: str) -> str:
    """A client's ISO timestamp in the stored fixed-width UTC form. ValueError when it is not one."""
    d = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "+"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def body_of(md: str) -> str:
    """The note after its YAML frontmatter."""
    m = re.match(r"---\n.*?\n---\n?", md, re.S)
    return (md[m.end():] if m else md).strip()


# ── writes (beam) ────────────────────────────────────────────────────────────

def save_note(user_id: str, note_id: str, device: str, title: str, source: str, created: str, md: str, ts: float) -> None:
    db.execute(
        "INSERT INTO pocket_notes (user_id, id, device, title, source, created, md, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, id) DO UPDATE SET device = excluded.device, title = excluded.title, "
        "source = excluded.source, created = excluded.created, md = excluded.md, ts = excluded.ts",
        (user_id, note_id, device, title, source, created, md, iso_from_epoch(ts)))


SURFACES = ("pocket", "web", "tui", "desktop")


def _open(user_id: str) -> dict | None:
    """The member's one open conversation, whichever surface opened it: that is what a surface picks up."""
    return db.query_one("SELECT id FROM pocket_conversations WHERE user_id = ? AND ended_at IS NULL "
                        "ORDER BY started_at DESC LIMIT 1", (user_id,))


def _start(user_id: str, device: str, surface: str) -> str:
    """Open a conversation. A member has one open at a time, so any other is ended first."""
    db.execute("UPDATE pocket_conversations SET ended_at = ? WHERE user_id = ? AND ended_at IS NULL",
               (db.now_ts(), user_id))
    cid = secrets.token_hex(16)
    db.execute("INSERT INTO pocket_conversations (id, user_id, device, started_at, origin) VALUES (?, ?, ?, ?, ?)",
               (cid, user_id, device, db.now_ts(), surface))
    return cid


def _add(user_id: str, cid: str, surface: str, pairs: list[tuple[str, str]]) -> None:
    for role, text in pairs:
        db.execute("INSERT INTO pocket_turns (id, conversation_id, user_id, role, text, ts, surface) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (secrets.token_hex(16), cid, user_id, role, text, db.now_ts(), surface))


def append_turns(user_id: str, device: str, pairs: list[tuple[str, str]], surface: str = "pocket") -> str:
    """Add (role, text) pairs to the member's open conversation (any surface's), opening one if needed."""
    row = _open(user_id)
    cid = row["id"] if row else _start(user_id, device, surface)
    _add(user_id, cid, surface, pairs)
    return cid


def new_conversation(user_id: str, device: str, surface: str = "pocket") -> str:
    """End the open conversation and open a fresh one; returns its id. An open conversation with no
    turns yet is reused, so repeated new_convo does not pile up empty rows."""
    row = _open(user_id)
    if row and db.query_one("SELECT 1 AS x FROM pocket_turns WHERE conversation_id = ? LIMIT 1", (row["id"],)) is None:
        return row["id"]
    return _start(user_id, device, surface)


def owns(user_id: str, cid: str) -> bool:
    return db.query_one("SELECT 1 AS x FROM pocket_conversations WHERE user_id = ? AND id = ?", (user_id, cid)) is not None


def resume(user_id: str, cid: str) -> bool:
    """Make this conversation the open one (continue it on another surface). False when it is not the member's."""
    if not owns(user_id, cid):
        return False
    if (_open(user_id) or {}).get("id") != cid:
        db.execute("UPDATE pocket_conversations SET ended_at = ? WHERE user_id = ? AND ended_at IS NULL AND id <> ?",
                   (db.now_ts(), user_id, cid))
        db.execute("UPDATE pocket_conversations SET ended_at = NULL WHERE user_id = ? AND id = ?", (user_id, cid))
    return True


def add_turns(user_id: str, cid: str, surface: str, pairs: list[tuple[str, str]]) -> bool:
    """Append to a named conversation from a surface; it becomes the open one, so a Pocket carries on from it."""
    if not resume(user_id, cid):
        return False
    _add(user_id, cid, surface, pairs)
    return True


def _origin(cid: str, started: str) -> str:
    """The surface that started it, or 'blended' once turns came from more than one."""
    seen = {r["surface"] for r in db.query("SELECT DISTINCT surface FROM pocket_turns WHERE conversation_id = ?", (cid,))}
    return "blended" if len(seen) > 1 else (next(iter(seen)) if seen else started)


# ── reads and deletes (routes) ───────────────────────────────────────────────

def _note_row(r: dict) -> dict:
    return {"id": r["id"], "title": r["title"], "source": r["source"], "created": r["created"],
            "device": r["device"], "ts": r["ts"]}


def list_notes(user_id: str, limit: int, before: str | None) -> list[dict]:
    sql, params = "SELECT * FROM pocket_notes WHERE user_id = ?", [user_id]
    if before:
        sql, params = sql + " AND ts < ?", params + [before]
    rows = db.query(sql + " ORDER BY ts DESC, id DESC LIMIT ?", tuple(params + [limit]))
    return [{**_note_row(r), "preview": body_of(r["md"])[:PREVIEW]} for r in rows]


def get_note(user_id: str, note_id: str) -> dict | None:
    r = db.query_one("SELECT * FROM pocket_notes WHERE user_id = ? AND id = ?", (user_id, note_id))
    return {**_note_row(r), "md": r["md"]} if r else None


def delete_note(user_id: str, note_id: str) -> bool:
    return db.execute("DELETE FROM pocket_notes WHERE user_id = ? AND id = ?", (user_id, note_id)) > 0


def list_conversations(user_id: str, limit: int, before: str | None) -> list[dict]:
    """Conversations with at least one turn, newest first."""
    sql = ("SELECT c.id, c.device, c.started_at, c.ended_at, c.origin, "
           "(SELECT COUNT(*) FROM pocket_turns t WHERE t.conversation_id = c.id) AS turns "
           "FROM pocket_conversations c WHERE c.user_id = ? "
           "AND EXISTS (SELECT 1 FROM pocket_turns t WHERE t.conversation_id = c.id)")
    params = [user_id]
    if before:
        sql, params = sql + " AND c.started_at < ?", params + [before]
    rows = db.query(sql + " ORDER BY c.started_at DESC, c.id DESC LIMIT ?", tuple(params + [limit]))
    for r in rows:
        first = db.query_one("SELECT text FROM pocket_turns WHERE conversation_id = ? AND role = 'user' "
                             "ORDER BY ts, id LIMIT 1", (r["id"],))
        r["preview"] = (first["text"] if first else "")[:PREVIEW]
        r["origin"] = _origin(r["id"], r["origin"])
    return rows


def get_conversation(user_id: str, cid: str) -> dict | None:
    c = db.query_one("SELECT id, device, started_at, ended_at, origin FROM pocket_conversations WHERE user_id = ? AND id = ?",
                     (user_id, cid))
    if not c:
        return None
    c["turns"] = db.query("SELECT role, text, ts, surface FROM pocket_turns WHERE conversation_id = ? AND user_id = ? "
                          "ORDER BY ts, id", (cid, user_id))
    c["origin"] = _origin(cid, c["origin"])
    return c


def delete_conversation(user_id: str, cid: str) -> bool:
    """Turns go with it (ON DELETE CASCADE where enforced; deleted explicitly so SQLite without foreign_keys agrees)."""
    if not db.query_one("SELECT 1 AS x FROM pocket_conversations WHERE user_id = ? AND id = ?", (user_id, cid)):
        return False
    db.execute("DELETE FROM pocket_turns WHERE conversation_id = ? AND user_id = ?", (cid, user_id))
    return db.execute("DELETE FROM pocket_conversations WHERE user_id = ? AND id = ?", (user_id, cid)) > 0
