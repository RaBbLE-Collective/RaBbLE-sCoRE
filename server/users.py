import os
import json
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, MultiFernet
from pydantic import BaseModel


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


def create_user(profile: UserProfile) -> UserProfile:
    path = get_data_dir() / "users" / f"{profile.id}.json"
    path.write_text(profile.model_dump_json())
    return profile


def get_user(user_id: str) -> Optional[UserProfile]:
    path = get_data_dir() / "users" / f"{user_id}.json"
    if not path.exists():
        return None
    return UserProfile.model_validate_json(path.read_text())


def get_user_by_handle(handle: str) -> Optional[UserProfile]:
    users_dir = get_data_dir() / "users"
    for f in users_dir.glob("*.json"):
        profile = UserProfile.model_validate_json(f.read_text())
        if profile.handle == handle:
            return profile
    return None


def get_user_by_api_key(api_key: str) -> Optional[UserProfile]:
    users_dir = get_data_dir() / "users"
    for f in users_dir.glob("*.json"):
        profile = UserProfile.model_validate_json(f.read_text())
        if profile.api_key == api_key:
            return profile
    return None


def save_user(profile: UserProfile) -> None:
    path = get_data_dir() / "users" / f"{profile.id}.json"
    path.write_text(profile.model_dump_json())


def list_handles() -> set[str]:
    users_dir = get_data_dir() / "users"
    handles = set()
    for f in users_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if "handle" in data:
                handles.add(data["handle"])
        except (json.JSONDecodeError, KeyError):
            pass
    return handles


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
    path = get_data_dir() / "invites" / f"{token_str}.json"
    path.write_text(invite.model_dump_json())
    return invite


def get_invite(token: str) -> Optional[InviteToken]:
    path = get_data_dir() / "invites" / f"{token}.json"
    if not path.exists():
        return None
    return InviteToken.model_validate_json(path.read_text())


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
    invite.consumed = True
    invite.consumed_by = user_id
    path = get_data_dir() / "invites" / f"{token}.json"
    path.write_text(invite.model_dump_json())
    return True


def list_invites() -> list[InviteToken]:
    invites_dir = get_data_dir() / "invites"
    result = []
    for f in invites_dir.glob("*.json"):
        try:
            result.append(InviteToken.model_validate_json(f.read_text()))
        except Exception:
            pass
    return result


# Bootstrap data directories at import time — idempotent
get_data_dir()
