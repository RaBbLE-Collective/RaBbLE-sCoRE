import os
import time
import secrets
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

import auth

logger = logging.getLogger("rabble.auth")

router = APIRouter(prefix="/api/v1/auth")

# In-memory user store — swap for a DB in production
_users_by_key: dict[str, dict] = {}  # api_key  -> user record
_users_by_id:  dict[str, dict] = {}  # user_id  -> user record
_users_by_email: dict[str, dict] = {}  # email   -> user record

_ADMIN_KEY = os.getenv("RABBLE_ADMIN_KEY", "")
_ADMIN_USER = {"id": "admin", "name": "Admin", "tier": "admin"}


class RegisterRequest(BaseModel):
    name:  str
    email: EmailStr


class LoginRequest(BaseModel):
    api_key: str


@router.post("/register", status_code=201)
async def register(body: RegisterRequest):
    """Create a new user account. Returns a permanent API key."""
    email_lower = body.email.lower().strip()
    if email_lower in _users_by_email:
        raise HTTPException(409, "Email already registered")

    user_id = secrets.token_urlsafe(12)
    api_key = f"rbbl_{secrets.token_urlsafe(32)}"
    user = {
        "id":         user_id,
        "name":       body.name.strip(),
        "email":      email_lower,
        "tier":       "free",
        "api_key":    api_key,
        "created_at": time.time(),
    }
    _users_by_key[api_key]     = user
    _users_by_id[user_id]      = user
    _users_by_email[email_lower] = user

    logger.info("user registered user_id=%s email=%s", user_id, email_lower)
    return {
        "api_key": api_key,
        "user_id": user_id,
        "name":    user["name"],
        "tier":    user["tier"],
        "message": "Store your API key securely — it will not be shown again.",
    }


@router.post("/token")
async def get_token(body: LoginRequest):
    """Exchange an API key for a short-lived JWT (8 hours)."""
    user = verify_api_key(body.api_key)
    if not user:
        raise HTTPException(401, "Invalid API key")
    token = auth.make_token(user["id"], user["name"], user.get("tier", "free"))
    return {
        "token":      token,
        "expires_in": 8 * 3600,
        "name":       user["name"],
        "tier":       user.get("tier", "free"),
    }


@router.get("/me")
async def me(user: dict = __import__("fastapi").Depends(auth.require_user)):
    return {"user_id": user["sub"], "name": user["name"], "tier": user.get("tier", "free")}


def verify_api_key(key: str) -> dict | None:
    if _ADMIN_KEY and key == _ADMIN_KEY:
        return _ADMIN_USER
    return _users_by_key.get(key)
