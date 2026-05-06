import os
import time
import secrets
from typing import Optional

import bcrypt
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

SECRET    = os.getenv("JWT_SECRET") or secrets.token_hex(32)
ALGORITHM = "HS256"
TTL       = 60 * 60 * 8  # 8 hours

_bearer = HTTPBearer(auto_error=False)

DEMO_MODE = os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes")

# Guest identity returned when DEMO_MODE is on and no credentials provided
_GUEST = {"sub": "guest", "name": "Guest", "tier": "free"}


def make_token(user_id: str, name: str, tier: str = "free") -> str:
    return jwt.encode(
        {"sub": user_id, "name": name, "tier": tier, "exp": time.time() + TTL},
        SECRET,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")


async def require_user(
    creds:     Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> dict:
    # Bearer JWT
    if creds:
        return decode_token(creds.credentials)

    # API key header
    if x_api_key:
        from auth_routes import verify_api_key  # avoid circular at module load
        user = verify_api_key(x_api_key)
        if user:
            return {"sub": user["id"], "name": user["name"], "tier": user.get("tier", "free")}
        raise HTTPException(401, "Invalid API key")

    # Demo mode — unauthenticated access as guest
    if DEMO_MODE:
        return _GUEST

    raise HTTPException(
        401,
        "Authentication required. Provide a Bearer token or X-API-Key header.",
    )


# ── bcrypt helpers ─────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())
