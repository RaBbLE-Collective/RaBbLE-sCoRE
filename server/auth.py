import os
import time
import secrets
from typing import Optional

import bcrypt
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# Required, no fallback: it signs every JWT and keys the stored BYO-key encryption
# (users.py). Missing or short = refuse to start. Local: server/.env via harness/env_gen.sh.
SECRET = os.getenv("JWT_SECRET", "")
if len(SECRET) < 16:
    raise RuntimeError(
        "JWT_SECRET is unset or shorter than 16 chars. Set it in server/.env "
        "(python -c 'import secrets; print(secrets.token_hex(32))')."
    )
ALGORITHM = "HS256"
TTL       = 60 * 60 * 8  # 8 hours

_bearer = HTTPBearer(auto_error=False)

DEMO_MODE = os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes")

# DEMO_MODE guests are loopback-only unless this is also set. Cloud (public demo) sets it;
# a laptop never does. See is_loopback_client / startup_guard.
DEMO_MODE_PUBLIC = os.getenv("DEMO_MODE_PUBLIC", "").lower() in ("1", "true", "yes")

_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

# Guest identity returned when DEMO_MODE is on and no credentials provided.
# llm_backend pins guests to Groq so the default chains never reach the CLI providers
# (claude_code etc. run with the host's own account and filesystem access).
_GUEST = {"sub": "guest", "name": "Guest", "tier": "free", "llm_backend": "hosted_groq"}


def is_loopback_client(request: Request) -> bool:
    """True only for a direct loopback connection. A forwarding header means a local
    reverse proxy/tunnel is relaying someone else, so it does not count."""
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        return False
    return bool(request.client) and request.client.host in _LOOPBACK


def _bind_hosts(argv: list[str]) -> list[str]:
    hosts = [os.getenv("BIND_HOST", ""), os.getenv("HOST", "")]
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            hosts.append(argv[i + 1])
        elif a.startswith("--host="):
            hosts.append(a[len("--host="):])
    return [h.strip() for h in hosts if h.strip()]


def startup_guard(argv: list[str] | None = None) -> None:
    """Refuse DEMO_MODE on a non-loopback bind unless DEMO_MODE_PUBLIC is explicit.
    The bind host is only visible via --host in argv or a BIND_HOST/HOST env var;
    the request-time loopback check in require_user covers everything else."""
    if not DEMO_MODE or DEMO_MODE_PUBLIC:
        return
    import sys
    bad = [h for h in _bind_hosts(sys.argv if argv is None else argv) if h not in _LOOPBACK]
    if bad:
        raise RuntimeError(
            f"DEMO_MODE=true while bound to {bad[0]}: unauthenticated guests would be reachable "
            "off-machine. Bind 127.0.0.1, set DEMO_MODE=false, or set DEMO_MODE_PUBLIC=true "
            "for a deliberately public demo."
        )


def make_token(user_id: str, name: str, tier: str = "free", handle: str = "") -> str:
    return jwt.encode(
        {"sub": user_id, "name": name, "handle": handle, "tier": tier, "exp": time.time() + TTL},
        SECRET,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")


async def require_user(
    request:   Request,
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
            return {
                "sub":    user["id"],
                "name":   user["name"],
                "handle": user.get("handle", ""),
                "tier":   user.get("tier", "free"),
            }
        raise HTTPException(401, "Invalid API key")

    # Demo mode: unauthenticated guest, loopback only unless DEMO_MODE_PUBLIC
    if DEMO_MODE and (DEMO_MODE_PUBLIC or is_loopback_client(request)):
        return dict(_GUEST)

    raise HTTPException(
        401,
        "Authentication required. Provide a Bearer token or X-API-Key header.",
    )


# ── bcrypt helpers ─────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())
