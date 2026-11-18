"""
The account event log, durable in the database (audit.jsonl on Render is ephemeral): who signed up, signed in
(or failed to), reset a password, paired or revoked a device, minted or redeemed an invite, had a limit changed,
or hit a limit. Table: account_events (migration 0007_usage.sql). Read by GET /api/v1/admin/events.

record() never raises and never blocks a request on a failing database: it logs and returns False.
user_id is the account when known (None otherwise). ip_hash is the one-way hash of the caller's address
(accounts_routes._h), never the address. detail is short text or a dict (stored as JSON); never put a
password, token or key in it.
"""

import json
import logging
import secrets

import db

logger = logging.getLogger("rabble.events")

MAX_DETAIL = 500


def record(user_id: str | None, event: str, *, ip_hash: str | None = None, detail=None) -> bool:
    try:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str, sort_keys=True)
        if detail is not None:
            detail = detail[:MAX_DETAIL]
        db.execute("INSERT INTO account_events (id, ts, user_id, event, ip_hash, detail) VALUES (?, ?, ?, ?, ?, ?)",
                   (secrets.token_hex(12), db.now_ts(), user_id, event[:64], ip_hash, detail))
        return True
    except Exception as exc:
        logger.warning("account event %r not recorded: %s", event, type(exc).__name__)
        return False


def recent(user_id: str | None = None, limit: int = 100) -> list[dict]:
    """Newest first. user_id narrows to one account."""
    limit = max(1, min(int(limit), 500))
    if user_id:
        rows = db.query("SELECT * FROM account_events WHERE user_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                        (user_id, limit))
    else:
        rows = db.query("SELECT * FROM account_events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
    return rows
