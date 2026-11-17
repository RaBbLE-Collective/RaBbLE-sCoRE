"""
Device keys (AC7): a member pairs a Pocket by minting a key tied to their account. Storage and rules live
here; the HTTP surface is devices_routes.py. Design: Grimoire log/plans/Accounts-Architect-and-Entity-Roadmap.md R6.

A key is "rbdv_" + 32 url-safe bytes, shown once; only its sha256 is stored. Using it resolves to the OWNER
(id, name, handle and tier read live from users, so a tier change applies) plus a device_id, so usage counts
against the owner. A revoked device, a missing owner or a guest owner is refused. Device keys are long-lived:
password reset and logout (accounts.revoke_all) do not end them, only the owner revoking the device does.
A device never reaches /admin (its tier is capped below admin and _require_admin refuses a device_id) and
cannot mint devices.

Env (read per call):
  DEVICE_LIMIT   max active (not revoked) devices per member, default 3, minimum 1. Invalid value = 3.
                 Admin-tier accounts are exempt (no limit).
"""

import hashlib
import os
import secrets
import time
from datetime import datetime, timezone

import db
import users as user_store

PREFIX = "rbdv_"
LAST_SEEN_EVERY = 60.0     # seconds: last_seen is written at most this often per device
_touched: dict[str, float] = {}


def limit() -> int:
    try:
        return max(1, int(os.getenv("DEVICE_LIMIT", "3")))
    except ValueError:
        return 3


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _public(row: dict) -> dict:
    return {"id": row["id"], "label": row["label"], "created_at": row["created_at"],
            "last_seen": row["last_seen"], "revoked": bool(row["revoked"])}


def is_exempt(user_id: str) -> bool:
    """Admin-tier accounts have no DEVICE_LIMIT."""
    owner = user_store.get_user(user_id)
    return bool(owner and owner.tier == "admin")


def active_count(user_id: str) -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM devices WHERE user_id = ? AND revoked = 0", (user_id,))["n"]


def create(user_id: str, label: str) -> dict | None:
    """Mint a device. Returns {id, label, key, created_at}, or None when the member is at DEVICE_LIMIT.
    Admin accounts (the Architect) are exempt, so dev boards can be paired without a cap."""
    if not is_exempt(user_id) and active_count(user_id) >= limit():
        return None
    key = PREFIX + secrets.token_urlsafe(32)
    row = {"id": secrets.token_hex(8), "label": label, "created_at": _now()}
    db.execute("INSERT INTO devices (id, user_id, label, key_hash, created_at, last_seen, revoked) VALUES (?, ?, ?, ?, ?, '', 0)",
               (row["id"], user_id, label, _hash(key), row["created_at"]))
    return {**row, "key": key}


def list_for(user_id: str) -> list[dict]:
    return [_public(r) for r in db.query("SELECT * FROM devices WHERE user_id = ? ORDER BY created_at, id", (user_id,))]


def revoke(user_id: str, device_id: str) -> bool:
    """True if the caller owns the device (revoking twice is fine); False if it is not theirs or does not exist."""
    row = db.query_one("SELECT id FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id))
    if not row:
        return False
    db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,))
    _touched.pop(device_id, None)
    return True


def resolve(key: str) -> dict | None:
    """A device key to its owner: {id, name, handle, tier, device_id}, or None (not a device key, revoked,
    owner gone, owner a guest). The tier is capped at pro: a device never carries admin."""
    if not key.startswith(PREFIX):
        return None
    dev = db.query_one("SELECT id, user_id, revoked FROM devices WHERE key_hash = ?", (_hash(key),))
    if not dev or dev["revoked"]:
        return None
    owner = user_store.get_user(dev["user_id"])
    if not owner or owner.tier == "guest":
        return None
    now = time.monotonic()
    if now - _touched.get(dev["id"], -LAST_SEEN_EVERY) >= LAST_SEEN_EVERY:
        _touched[dev["id"]] = now
        db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (_now(), dev["id"]))
    tier = "pro" if owner.tier == "admin" else owner.tier
    return {"id": owner.id, "name": owner.display_name, "handle": owner.handle, "tier": tier, "device_id": dev["id"]}
