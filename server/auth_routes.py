import os
import re
import hmac
import secrets
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header, Request
from pydantic import BaseModel, EmailStr

import auth
import db
import groqmeter
import guests
import snapshot
import storage
import users as user_store
from users import UserProfile, InviteToken

logger = logging.getLogger("rabble.auth")

router = APIRouter(prefix="/api/v1/auth")
member_router = APIRouter(prefix="/api/v1")

_ADMIN_KEY = os.getenv("RABBLE_ADMIN_KEY", "")
_ADMIN_USER = {"id": "admin", "name": "Admin", "handle": "admin", "tier": "admin"}

_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,23}$")
_VALID_BACKENDS = {
    "hosted_openrouter",
    "hosted_groq",
    "byo_openrouter",
    "byo_openai",
    "byo_anthropic",
}


# ── Request models ─────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name:  str
    email: EmailStr


class LoginRequest(BaseModel):
    api_key: str


class InviteCreateRequest(BaseModel):
    note: Optional[str] = None
    expires_days: int = 7


class GuestRequest(BaseModel):
    label: Optional[str] = None


class SummonRequest(BaseModel):
    invite_token: str
    handle: str
    display_name: str
    intention: str
    llm_backend: str = "hosted_openrouter"
    byo_key: Optional[str] = None


class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    intention: Optional[str] = None
    llm_backend: Optional[str] = None
    byo_key: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _require_admin(request: Request,
                         x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
                         creds=Depends(auth._bearer),
                         x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> dict:
    # A valid X-Admin-Key is enough on its own; otherwise the caller must be an admin-tier user.
    if _ADMIN_KEY and x_admin_key and hmac.compare_digest(x_admin_key.encode(), _ADMIN_KEY.encode()):
        return _ADMIN_USER
    user = await auth.require_user(request, creds, x_api_key)   # 401 without credentials
    if user.get("tier") == "admin":
        return user
    raise HTTPException(403, "Admin access required")


def _profile_to_dict(profile: UserProfile) -> dict:
    d = profile.model_dump()
    d.pop("byo_key_enc", None)
    d.pop("api_key", None)
    return d


# ── Legacy /auth/* endpoints (backward compat) ─────────────────────────────────

@router.post("/register", status_code=201)
async def register(body: RegisterRequest):
    """Legacy registration — creates a file-backed UserProfile with an auto-handle.
    Off unless ALLOW_REGISTER=true: it is open to anyone and returns a working API key."""
    if os.getenv("ALLOW_REGISTER", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(404, "Not found")
    base_handle = re.sub(r"[^a-z0-9]", "-", body.name.lower().strip())[:20].strip("-") or "user"
    handle = base_handle
    existing_handles = user_store.list_handles()
    suffix = 1
    while handle in existing_handles:
        handle = f"{base_handle[:18]}-{suffix}"
        suffix += 1

    user_id = secrets.token_urlsafe(12)
    api_key = f"rbbl_{secrets.token_urlsafe(32)}"
    profile = UserProfile(
        id=user_id,
        handle=handle,
        display_name=body.name.strip(),
        email=body.email.lower().strip(),
        join_date=datetime.now(timezone.utc).isoformat(),
        intention="",
        tier="free",
        api_key=api_key,
    )
    user_store.create_user(profile)
    logger.info("user registered user_id=%s handle=%s", user_id, handle)
    return {
        "api_key": api_key,
        "user_id": user_id,
        "name":    profile.display_name,
        "tier":    profile.tier,
        "message": "Store your API key securely — it will not be shown again.",
    }


@router.post("/token")
async def get_token(body: LoginRequest):
    """Exchange an API key for a short-lived JWT (8 hours)."""
    user = verify_api_key(body.api_key)
    if not user:
        raise HTTPException(401, "Invalid API key")
    token = auth.make_token(user["id"], user["name"], user.get("tier", "free"), user.get("handle", ""))
    return {
        "token":      token,
        "expires_in": 8 * 3600,
        "name":       user["name"],
        "tier":       user.get("tier", "free"),
    }


@router.get("/me")
async def me(user: dict = Depends(auth.require_user)):
    return {"user_id": user["sub"], "name": user["name"], "tier": user.get("tier", "free")}


# ── Admin: create invite ───────────────────────────────────────────────────────

@member_router.post("/admin/invites", status_code=201)
async def create_invite(body: InviteCreateRequest, admin: dict = Depends(_require_admin)):
    invite = user_store.create_invite(
        created_by=admin["id"],
        note=body.note or "",
        expires_days=body.expires_days,
    )
    base_url = os.getenv("FRONTEND_URL", "https://joinrabble.world")
    return {
        "token":      invite.token,
        "url":        f"{base_url}/summon/{invite.token}",
        "expires_at": invite.expires_at,
        "note":       invite.note,
    }


# ── Admin: inventory and export (counts and sizes only, no content) ────────────


def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "data"))


def _transcript_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "/tmp/rabble-data")) / "transcripts"


def _line_count(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


@member_router.get("/admin/inventory")
async def inventory(admin: dict = Depends(_require_admin)):
    import audit
    by_tier = {r["tier"]: r["n"] for r in db.query("SELECT tier, COUNT(*) AS n FROM users GROUP BY tier")}
    invites = db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(consumed), 0) AS used FROM invites") or {}
    db_file = db.db_path()
    tdir = _transcript_dir()
    files = sorted(tdir.glob("*")) if tdir.is_dir() else []
    nested = [f for f in tdir.glob("*/*.json")] if tdir.is_dir() else []
    return {
        "users": {"total": sum(by_tier.values()), "by_tier": by_tier, "guests": by_tier.get("guest", 0)},
        "invites": {"total": invites.get("n", 0), "consumed": int(invites.get("used", 0))},
        "guest_turns_today": guests.turns_today(),
        "guest_daily_limit": guests.daily_limit(),
        "db": {"path": db_file.name, "bytes": db_file.stat().st_size if db_file.exists() else 0},
        "transcripts": {
            "files": [{"name": f.name, "bytes": f.stat().st_size} for f in files if f.is_file()],
            "entries": sum(_line_count(f) for f in files if f.suffix == ".jsonl") + len(nested),
        },
        "audit_lines": _line_count(Path(audit.LOG_PATH)),
        "r2": {
            "mode": storage.mode(),
            "usage": storage.usage(),
            "budgets": storage.budgets(),
            "exhausted": storage.exhausted(),
            "last_snapshot": db.meta_get("last_snapshot"),
            "restore": snapshot.state,
            "uploads_enabled": snapshot.uploads_enabled() and storage.configured(),
            "snapshots": snapshot.status(),
        },
        "groq": groqmeter.state(),
    }


@member_router.get("/admin/export")
async def export(admin: dict = Depends(_require_admin)):
    """A tar.gz to pull before a redeploy: db snapshot, the DATA_DIR audit log, transcripts on disk.
    Holds hashed API keys, BYO keys encrypted under JWT_SECRET and conversation text: admin only,
    and treat the file as a secret."""
    import audit
    import tarfile
    import tempfile
    from starlette.background import BackgroundTask
    from fastapi.responses import FileResponse
    out = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    out.close()
    try:
        with tempfile.TemporaryDirectory() as d, tarfile.open(out.name, mode="w:gz") as tar:
            snap = Path(d) / "score.db"
            db.backup(snap)
            tar.add(snap, arcname="score.db")
            log = Path(audit.LOG_PATH)
            if log.is_file() and log.resolve().parent == _data_dir().resolve():
                tar.add(log, arcname="audit.jsonl")     # only the DATA_DIR log, never an arbitrary path
            if _transcript_dir().is_dir():
                tar.add(_transcript_dir(), arcname="transcripts")
    except Exception:
        os.unlink(out.name)
        raise
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return FileResponse(out.name, media_type="application/gzip", filename=f"score-export-{stamp}.tar.gz",
                        background=BackgroundTask(os.unlink, out.name))


# ── Public: summon (register via invite) ───────────────────────────────────────

@member_router.post("/users/summon", status_code=201)
async def summon(body: SummonRequest):
    if not _HANDLE_RE.match(body.handle):
        raise HTTPException(422, "handle must be 1–24 lowercase alphanumeric + hyphens, not starting with hyphen")

    if body.llm_backend not in _VALID_BACKENDS:
        raise HTTPException(422, f"llm_backend must be one of: {', '.join(sorted(_VALID_BACKENDS))}")

    if body.llm_backend.startswith("byo_") and not body.byo_key:
        raise HTTPException(422, f"byo_key required when llm_backend is {body.llm_backend}")

    if len(body.intention) > 500:
        raise HTTPException(422, "intention must be ≤500 characters")

    invite = user_store.get_invite(body.invite_token)
    if not invite:
        raise HTTPException(400, "Invalid invite token")

    if body.handle in user_store.list_handles():
        raise HTTPException(409, f"Handle '{body.handle}' is already taken")

    user_id = secrets.token_urlsafe(12)
    api_key = f"rbbl_{secrets.token_urlsafe(32)}"

    byo_key_enc = None
    if body.byo_key:
        byo_key_enc = user_store.encrypt_key(body.byo_key)

    profile = UserProfile(
        id=user_id,
        handle=body.handle,
        display_name=body.display_name.strip(),
        join_date=datetime.now(timezone.utc).isoformat(),
        intention=body.intention,
        llm_backend=body.llm_backend,
        byo_key_enc=byo_key_enc,
        tier="collective",
        api_key=api_key,
    )
    user_store.create_user(profile)

    # Mark invite consumed after successful user creation
    if not user_store.consume_invite(body.invite_token, user_id):
        raise HTTPException(400, "Invite token is expired or already used")

    import sessions as session_store
    session = session_store.create_session(user_id)

    token = auth.make_token(user_id, profile.display_name, profile.tier, profile.handle)
    logger.info("user summoned user_id=%s handle=%s", user_id, body.handle)
    return {
        "api_key":    api_key,
        "user_id":    user_id,
        "handle":     body.handle,
        "token":      token,
        "session_id": session.session_id,
    }


# ── Public: per-device guest (issued to a holder of GUEST_ISSUE_SECRET) ─────────

@member_router.post("/users/guest", status_code=201)
async def issue_guest(body: Optional[GuestRequest] = None,
                      x_guest_secret: Optional[str] = Header(default=None, alias="X-Guest-Secret")):
    secret = guests.issue_secret()
    if not secret:
        raise HTTPException(404, "Not found")
    if not x_guest_secret or not hmac.compare_digest(x_guest_secret.encode(), secret.encode()):
        raise HTTPException(403, "Invalid guest secret")
    spent = storage.exhausted()
    if spent:
        raise HTTPException(503, "Guest sign-up is paused: this month's storage allowance is used up. "
                                 "It resumes next month.")
    if not guests.take_issue_slot():
        raise HTTPException(429, f"Too many guest accounts issued this hour (limit {guests.issue_limit()}). "
                                 "Try again later.", headers={"Retry-After": "3600"})
    profile, api_key = guests.create_guest(body.label if body and body.label else "")
    logger.info("guest issued user_id=%s handle=%s", profile.id, profile.handle)
    return {"api_key": api_key, "handle": profile.handle}


# ── Authenticated: profile ─────────────────────────────────────────────────────

@member_router.get("/users/me")
async def get_profile(user: dict = Depends(auth.require_user)):
    profile = user_store.get_user(user["sub"])
    if not profile:
        raise HTTPException(404, "Profile not found")
    return _profile_to_dict(profile)


@member_router.put("/users/me")
async def update_profile(body: ProfileUpdateRequest, user: dict = Depends(auth.require_user)):
    if guests.is_guest(user):
        raise HTTPException(403, "Guest accounts cannot edit their profile.")
    profile = user_store.get_user(user["sub"])
    if not profile:
        raise HTTPException(404, "Profile not found")

    if body.display_name is not None:
        profile.display_name = body.display_name.strip()

    if body.intention is not None:
        if len(body.intention) > 500:
            raise HTTPException(422, "intention must be ≤500 characters")
        profile.intention = body.intention

    if body.llm_backend is not None:
        if body.llm_backend not in _VALID_BACKENDS:
            raise HTTPException(422, f"llm_backend must be one of: {', '.join(sorted(_VALID_BACKENDS))}")
        profile.llm_backend = body.llm_backend

    if body.byo_key is not None:
        profile.byo_key_enc = user_store.encrypt_key(body.byo_key)

    user_store.save_user(profile)
    return _profile_to_dict(profile)


# ── verify_api_key — called by auth.require_user ───────────────────────────────

def verify_api_key(key: str) -> dict | None:
    if _ADMIN_KEY and hmac.compare_digest(key.encode(), _ADMIN_KEY.encode()):
        return _ADMIN_USER
    profile = user_store.get_user_by_api_key(key)
    if profile:
        return {
            "id":     profile.id,
            "name":   profile.display_name,
            "handle": profile.handle,
            "tier":   profile.tier,
        }
    return None
