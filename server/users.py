import os
import logging
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, MultiFernet
from pydantic import BaseModel

import db

logger = logging.getLogger("rabble.users")


class UserProfile(BaseModel):
    id: str
    handle: str
    display_name: str
    email: Optional[str] = None
    join_date: str
    intention: str
    llm_backend: str = "hosted_openrouter"
    byo_key_enc: Optional[str] = None
    tier: str = "collective"
    api_key: str


class InviteToken(BaseModel):
    token: str
    created_by: str
    created_at: str
    expires_at: str
    note: Optional[str] = None
    consumed: bool = False
    consumed_by: Optional[str] = None


def get_data_dir() -> Path:
    base = Path(os.getenv("DATA_DIR", "data"))
    for subdir in ("users", "invites", "sessions"):
        (base / subdir).mkdir(parents=True, exist_ok=True)
    return base


def _fernet() -> MultiFernet:
    # Key = SHA-256(JWT_SECRET). auth.py refuses to start without the secret, so
    # there is no empty-key path. The legacy key (secret padded/truncated to 32
    # bytes) stays second so keys stored before S250 still decrypt.
    raw = os.getenv("JWT_SECRET", "").encode()
    if len(raw) < 16:
        raise RuntimeError("JWT_SECRET is unset or shorter than 16 chars")
    current = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    legacy  = base64.urlsafe_b64encode(raw[:32].ljust(32, b"\x00"))
    return MultiFernet([Fernet(current), Fernet(legacy)])


def encrypt_key(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_key(enc: str) -> str:
    return _fernet().decrypt(enc.encode()).decode()


# API keys are stored as sha256 (they are 256-bit random, so a plain hash is enough). A profile
# read back carries the hash in api_key; the plaintext exists only in the response that issues it.
_HASHED = "sha256:"


def hash_api_key(key: str) -> str:
    return key if key.startswith(_HASHED) else _HASHED + hashlib.sha256(key.encode()).hexdigest()


_USER_COLS = ("id", "handle", "display_name", "email", "join_date", "intention",
              "llm_backend", "byo_key_enc", "tier", "api_key")
_INVITE_COLS = ("token", "created_by", "created_at", "expires_at", "note", "consumed", "consumed_by")


def _user(row: dict | None) -> Optional[UserProfile]:
    return UserProfile(**row) if row else None


def _invite(row: dict | None) -> Optional[InviteToken]:
    return InviteToken(**{**row, "consumed": bool(row["consumed"])}) if row else None


def create_user(profile: UserProfile) -> UserProfile:
    save_user(profile)
    return profile


def get_user(user_id: str) -> Optional[UserProfile]:
    return _user(db.query_one("SELECT * FROM users WHERE id = ?", (user_id,)))


def get_user_by_handle(handle: str) -> Optional[UserProfile]:
    return _user(db.query_one("SELECT * FROM users WHERE handle = ?", (handle,)))


def get_user_by_api_key(api_key: str) -> Optional[UserProfile]:
    if api_key.startswith(_HASHED):
        return None   # a stored hash must never work as a credential
    return _user(db.query_one("SELECT * FROM users WHERE api_key = ?", (hash_api_key(api_key),)))


def save_user(profile: UserProfile) -> None:
    d = profile.model_dump()
    d["api_key"] = hash_api_key(d["api_key"])
    sets = ", ".join(f"{c} = excluded.{c}" for c in _USER_COLS[1:])
    db.execute(
        f"INSERT INTO users ({', '.join(_USER_COLS)}) VALUES ({', '.join('?' * len(_USER_COLS))}) "
        f"ON CONFLICT(id) DO UPDATE SET {sets}",
        tuple(d[c] for c in _USER_COLS),
    )


def list_handles() -> set[str]:
    return {r["handle"] for r in db.query("SELECT handle FROM users")}


def create_invite(created_by: str, note: str = "", expires_days: int = 7) -> InviteToken:
    token_str = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    invite = InviteToken(
        token=token_str,
        created_by=created_by,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(days=expires_days)).isoformat(),
        note=note or None,
    )
    _save_invite(invite)
    return invite


def _save_invite(invite: InviteToken) -> None:
    d = invite.model_dump()
    d["consumed"] = int(invite.consumed)
    sets = ", ".join(f"{c} = excluded.{c}" for c in _INVITE_COLS[1:])
    db.execute(
        f"INSERT INTO invites ({', '.join(_INVITE_COLS)}) VALUES ({', '.join('?' * len(_INVITE_COLS))}) "
        f"ON CONFLICT(token) DO UPDATE SET {sets}",
        tuple(d[c] for c in _INVITE_COLS),
    )


def get_invite(token: str) -> Optional[InviteToken]:
    return _invite(db.query_one("SELECT * FROM invites WHERE token = ?", (token,)))


def consume_invite(token: str, user_id: str) -> bool:
    invite = get_invite(token)
    if not invite:
        return False
    if invite.consumed:
        return False
    now = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(invite.expires_at)
    # Make expires_at offset-aware if it lacks tzinfo
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        return False
    # The WHERE keeps two racing consumers from both winning.
    return db.execute("UPDATE invites SET consumed = 1, consumed_by = ? WHERE token = ? AND consumed = 0",
                      (user_id, token)) == 1


def list_invites() -> list[InviteToken]:
    return [_invite(r) for r in db.query("SELECT * FROM invites")]


def _hash_existing_keys() -> None:
    """Rows from before hashing (or a restored older snapshot) are hashed in place."""
    for r in db.query("SELECT id, api_key FROM users WHERE api_key NOT LIKE 'sha256:%'"):
        db.execute("UPDATE users SET api_key = ? WHERE id = ?", (hash_api_key(r["api_key"]), r["id"]))


def _import_json() -> None:
    """Once per database: the users/ and invites/ JSON files from before the db are
    read in (left on disk, untouched). A db restored from R2 already carries the flag."""
    if db.meta_get("json_imported"):
        return
    base = Path(os.getenv("DATA_DIR", "data"))
    n_users = n_invites = 0
    for f in sorted((base / "users").glob("*.json")):
        try:
            p = UserProfile.model_validate_json(f.read_text())
        except Exception as exc:
            logger.warning("skipped unreadable user file %s: %s", f.name, exc)
            continue
        if get_user(p.id) is None:
            save_user(p)
            n_users += 1
    for f in sorted((base / "invites").glob("*.json")):
        try:
            i = InviteToken.model_validate_json(f.read_text())
        except Exception as exc:
            logger.warning("skipped unreadable invite file %s: %s", f.name, exc)
            continue
        if get_invite(i.token) is None:
            _save_invite(i)
            n_invites += 1
    db.meta_set("json_imported", datetime.now(timezone.utc).isoformat())
    if n_users or n_invites:
        logger.info("imported %d users, %d invites from JSON files", n_users, n_invites)


db.register_init(_import_json)
db.register_init(_hash_existing_keys)

# Bootstrap data directories at import time — idempotent
get_data_dir()
