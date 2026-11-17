"""
Sign-in accounts (AC2): email + username + password, Argon2id, single-use email tokens, rotating
refresh tokens. Storage and crypto live here; the HTTP surface is accounts_routes.py.

An account is a users row (tier "free", handle = lowercase username, an unusable random api_key)
plus an accounts row (hash, verified flag, sessions_valid_after). Tokens are stored as sha256 only.

Env (all read per call, so none needs a restart except JWT_SECRET):
  ACCOUNT_CAP              (0 closes signup) max non-guest accounts (users.tier != "guest"; with REQUIRE_VERIFIED_EMAIL only
                           verified ones count, and unverified accounts older than 7 days are purged); default 100. Past it, signup
                           answers with the persona waitlist line. Invalid value = 100.
  REQUIRE_VERIFIED_EMAIL   default true. When true, login is refused (403 email_not_verified) until the
                           emailed link is used. Signup itself always works and sends the verify mail.
  AUTH_LINK_BASE           base of the links in mail (/verify-email?token=, /reset-password?token=);
                           default FRONTEND_URL if it is one URL, else https://joinrabble.world.
  VERIFY_TTL_MIN           verify token life, default 1440 (24 h).
  RESET_TTL_MIN            reset token life, default 60.
  REFRESH_TTL_DAYS         refresh token / cookie life, default 30.
  AUTH_IP_HEADER           optional header naming the client IP behind a proxy (e.g. x-forwarded-for;
                           the LAST entry is used). Unset = the socket peer. Only set it behind a
                           proxy you control: a client can forge it otherwise.
  MAIL_PROVIDER / MAIL_*   see mailer.py. Unset = no mail is sent (NullMailer); "console" prints links, dev only.
  REFRESH_GRACE_SECONDS    a refresh token used again within this many seconds (two tabs, a retry) is not
                           treated as theft; default 10.
  SESSION_CACHE_SECONDS    how long an access JWT's revocation check is cached per account, default 5. A logout
                           or reuse on this instance takes effect at once; another instance sees it within this.
  JWT_SECRET               signs the 8 h access JWT (auth.py).
The refresh cookie is rabble_refresh: HttpOnly, Secure, SameSite=Lax, Path=/api/v1/auth.
"""

import hashlib
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

import db
import users as user_store
from users import UserProfile

logger = logging.getLogger("rabble.accounts")

_ph = PasswordHasher()   # Argon2id, library defaults, random per-hash salt
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,23}$")
_RESERVED = {"admin", "administrator", "root", "system", "guest", "rabble", "support", "me", "api", "null", "pocket"}
PW_MIN, PW_MAX = 10, 128
_dummy_hash: str | None = None


# ── env ────────────────────────────────────────────────────────────────────────

def _flag(name: str, default: bool) -> bool:
    """A flag that defaults on fails closed: only an explicit off value turns it off (a typo such as
    'ture' or a word such as 'enabled' keeps it on). A flag that defaults off needs an explicit on."""
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    if default:
        return v not in ("0", "false", "no", "off")
    return v in ("1", "true", "yes", "on")


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "")))
    except ValueError:
        return default


def account_cap() -> int:
    return _int("ACCOUNT_CAP", 100, minimum=0)      # 0 closes signup


def require_verified() -> bool:
    return _flag("REQUIRE_VERIFIED_EMAIL", True)


def link_base() -> str:
    base = os.getenv("AUTH_LINK_BASE", "").strip()
    if not base:
        fe = os.getenv("FRONTEND_URL", "").strip()
        base = fe if fe.startswith("http") and "," not in fe else "https://joinrabble.world"
    return base.rstrip("/")


def refresh_ttl() -> timedelta:
    return timedelta(days=_int("REFRESH_TTL_DAYS", 30))


# ── time ───────────────────────────────────────────────────────────────────────

def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _in(delta: timedelta) -> str:
    return _ts(datetime.now(timezone.utc) + delta)


def _epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ── passwords ──────────────────────────────────────────────────────────────────

def hash_pw(plain: str) -> str:
    return _ph.hash(plain)


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _ph.hash("dummy-password-for-timing-" + secrets.token_hex(8))
    return _dummy_hash


def check_pw(plain: str, stored: str) -> tuple[bool, bool]:
    """(matches, needs_rehash). Argon2id hashes; bcrypt ($2) only for legacy hashes, always rehashed."""
    try:
        if stored.startswith("$2"):
            ok = bcrypt.checkpw(plain.encode()[:72], stored.encode())
            return ok, ok
        _ph.verify(stored, plain)
        return True, _ph.check_needs_rehash(stored)
    except (VerificationError, InvalidHashError, ValueError):
        return False, False


def verify_or_dummy(plain: str, stored: Optional[str]) -> tuple[bool, bool]:
    """Always costs one Argon2 verify, so an unknown user takes the same path as a wrong password."""
    if stored is None:
        try:
            _ph.verify(_dummy(), plain)
        except (VerificationError, InvalidHashError):
            pass
        return False, False
    return check_pw(plain, stored)


def password_problem(pw: str, username: str, email: str) -> Optional[str]:
    if len(pw) < PW_MIN:
        return f"Password must be at least {PW_MIN} characters."
    if len(pw) > PW_MAX:
        return f"Password must be at most {PW_MAX} characters."
    low = pw.lower()
    if low in (username.lower(), email.lower(), email.lower().split("@")[0]):
        return "Password is too easy to guess (it matches your username or email)."
    if len(set(pw)) == 1:
        return "Password is too easy to guess."
    return None


def username_problem(name: str) -> Optional[str]:
    if not _USERNAME_RE.match(name):
        return "Username must be 3-24 characters: letters, digits, '-' or '_', starting with a letter or digit."
    if name.lower() in _RESERVED:
        return "That username is reserved."
    return None


# ── accounts ───────────────────────────────────────────────────────────────────

def count_accounts() -> int:
    """Accounts that use a slot under the cap: non-guest users; with verification required, only the
    verified ones (an unverified signup must not be able to fill the rooms)."""
    if require_verified():
        return int(db.query_one("SELECT COUNT(*) AS n FROM users u JOIN accounts a ON a.user_id = u.id "
                                "WHERE u.tier <> 'guest' AND a.email_verified = 1")["n"]) + \
               int(db.query_one("SELECT COUNT(*) AS n FROM users u WHERE u.tier <> 'guest' "
                                "AND u.id NOT IN (SELECT user_id FROM accounts)")["n"])
    return int(db.query_one("SELECT COUNT(*) AS n FROM users WHERE tier <> 'guest'")["n"])


def by_user_id(user_id: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))


def by_username(name: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM accounts WHERE username_lower = ?", (name.lower(),))


def by_email(email: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM accounts WHERE email_lower = ?", (email.lower().strip(),))


def by_identifier(ident: str) -> Optional[dict]:
    ident = ident.strip()
    return by_email(ident) if "@" in ident else by_username(ident)


def create_account(username: str, email: str, password: str) -> dict:
    """Insert accounts then users; a duplicate (pre-checked, and enforced by the unique indexes) raises
    ValueError('username'|'email'). If the users write fails, the accounts row is removed again."""
    email_l = email.strip().lower()
    if by_username(username) or username.lower() in user_store.list_handles():
        raise ValueError("username")
    if by_email(email_l):
        raise ValueError("email")
    uid = secrets.token_urlsafe(12)
    now = db.now_ts()
    try:
        db.execute(
            "INSERT INTO accounts (user_id, username, username_lower, email, email_lower, password_hash, "
            "email_verified, sessions_valid_after, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, '', ?, ?)",
            (uid, username, username.lower(), email_l, email_l, hash_pw(password), now, now))
    except Exception:
        if by_username(username):       # lost a race on a unique index
            raise ValueError("username")
        if by_email(email_l):
            raise ValueError("email")
        raise
    try:
        user_store.create_user(UserProfile(
            id=uid, handle=username.lower(), display_name=username, email=email_l,
            join_date=datetime.now(timezone.utc).isoformat(), intention="", llm_backend="hosted_groq",
            tier="free", api_key=f"rbbl_{secrets.token_urlsafe(32)}"))   # never shown: not a credential
    except Exception:
        db.execute("DELETE FROM accounts WHERE user_id = ?", (uid,))
        raise
    return by_user_id(uid)


def drop_unverified(email: str) -> bool:
    """Delete an account whose email was never verified (it proves nothing), with its users row and
    tokens, so the address and name are free for whoever really owns them. True if one was removed."""
    acct = by_email(email)
    if not acct or acct["email_verified"]:
        return False
    uid = acct["user_id"]
    for t in ("auth_tokens", "refresh_tokens"):
        db.execute(f"DELETE FROM {t} WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM accounts WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    _cache_drop(uid)
    return True


def username_taken(username: str, except_user_id: str = "") -> bool:
    a = by_username(username)
    if a and a["user_id"] != except_user_id:
        return True
    return username.lower() in user_store.list_handles() and not (a and a["user_id"] == except_user_id)


def set_password(user_id: str, password: str) -> None:
    db.execute("UPDATE accounts SET password_hash = ?, updated_at = ? WHERE user_id = ?",
               (hash_pw(password), db.now_ts(), user_id))


def mark_verified(user_id: str) -> None:
    db.execute("UPDATE accounts SET email_verified = 1, updated_at = ? WHERE user_id = ?", (db.now_ts(), user_id))


def claims_for(acct: dict) -> dict:
    prof = user_store.get_user(acct["user_id"])
    return {"id": acct["user_id"], "name": acct["username"], "handle": acct["username_lower"],
            "tier": prof.tier if prof else "free"}


# ── single-use email tokens ────────────────────────────────────────────────────

_TOKEN_TTL = {"verify": ("VERIFY_TTL_MIN", 1440), "reset": ("RESET_TTL_MIN", 60)}


def issue_token(user_id: str, kind: str) -> str:
    """A fresh token for the link; earlier unused ones of the same kind stop working."""
    name, default = _TOKEN_TTL[kind]
    db.execute("UPDATE auth_tokens SET consumed = 1 WHERE user_id = ? AND kind = ? AND consumed = 0", (user_id, kind))
    raw = secrets.token_urlsafe(32)
    db.execute("INSERT INTO auth_tokens (token_hash, user_id, kind, created_at, expires_at, consumed) VALUES (?, ?, ?, ?, ?, 0)",
               (_sha(raw), user_id, kind, db.now_ts(), _in(timedelta(minutes=_int(name, default)))))
    return raw


def peek_token(raw: str, kind: str) -> Optional[str]:
    """user_id if the token is live (unconsumed, unexpired); does not consume it."""
    row = db.query_one("SELECT user_id FROM auth_tokens WHERE token_hash = ? AND kind = ? AND consumed = 0 AND expires_at > ?",
                       (_sha(raw), kind, db.now_ts()))
    return row["user_id"] if row else None


def consume_token(raw: str, kind: str) -> Optional[str]:
    """Atomically spend a live token; user_id on success, None otherwise (second use, expired, unknown)."""
    uid = peek_token(raw, kind)
    if not uid:
        return None
    n = db.execute("UPDATE auth_tokens SET consumed = 1 WHERE token_hash = ? AND consumed = 0 AND expires_at > ?",
                   (_sha(raw), db.now_ts()))
    return uid if n == 1 else None


# ── refresh tokens and session revocation ──────────────────────────────────────

def issue_refresh(user_id: str, family: Optional[str] = None) -> str:
    raw = "rt_" + secrets.token_urlsafe(32)
    db.execute("INSERT INTO refresh_tokens (token_hash, user_id, family_id, created_at, expires_at, used, revoked) "
               "VALUES (?, ?, ?, ?, ?, 0, 0)",
               (_sha(raw), user_id, family or secrets.token_urlsafe(12), db.now_ts(), _in(refresh_ttl())))
    return raw


def _revoke_family(family: str) -> None:
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE family_id = ?", (family,))


def _bump_valid_after(user_id: str) -> None:
    """Every access JWT issued before now stops working (checked in session_valid)."""
    db.execute("UPDATE accounts SET sessions_valid_after = ?, updated_at = ? WHERE user_id = ?",
               (db.now_ts(), db.now_ts(), user_id))
    _cache_drop(user_id)


class RefreshRace(Exception):
    """The token was already rotated a moment ago (a second tab or a retry). Not theft: the caller
    answers 409 and leaves the cookie the first request just set."""


def rotate_refresh(raw: str) -> Optional[tuple[str, str]]:
    """(user_id, new refresh token) or None. A token that was already rotated (or lost a race to be)
    revokes its whole family and kills the access JWTs: someone is replaying a stolen copy.
    The winner claims the parent first, inserts the child, then re-reads the parent: a loser that
    revoked the family in between has revoked the parent, so the child is revoked too."""
    row = db.query_one("SELECT * FROM refresh_tokens WHERE token_hash = ?", (_sha(raw),))
    if not row or row["revoked"]:
        return None
    claimed = (not row["used"]) and db.execute(
        "UPDATE refresh_tokens SET used = 1, used_at = ? WHERE token_hash = ? AND used = 0 AND revoked = 0",
        (db.now_ts(), row["token_hash"])) == 1
    if not claimed:
        fresh = db.query_one("SELECT used_at FROM refresh_tokens WHERE token_hash = ?", (row["token_hash"],))
        grace = _int("REFRESH_GRACE_SECONDS", 10, minimum=0)
        if fresh and fresh["used_at"] and time.time() - _epoch(fresh["used_at"]) <= grace:
            raise RefreshRace()
        _revoke_family(row["family_id"])
        _bump_valid_after(row["user_id"])
        logger.warning("refresh token reuse detected user_id=%s", row["user_id"])
        return None
    if row["expires_at"] <= db.now_ts():
        return None
    child = issue_refresh(row["user_id"], row["family_id"])
    parent = db.query_one("SELECT revoked FROM refresh_tokens WHERE token_hash = ?", (row["token_hash"],))
    if not parent or parent["revoked"]:
        _revoke_family(row["family_id"])
        return None
    return row["user_id"], child


def revoke_presented(raw: str) -> None:
    """Logout: the presented token's family dies, and so do this user's access JWTs (their other
    devices get a new one from their own refresh cookie)."""
    row = db.query_one("SELECT family_id, user_id FROM refresh_tokens WHERE token_hash = ?", (_sha(raw),))
    if row:
        _revoke_family(row["family_id"])
        _bump_valid_after(row["user_id"])


def revoke_all(user_id: str) -> None:
    """Every refresh token dies, and every access JWT issued before now stops working."""
    db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
    _bump_valid_after(user_id)


# The revocation check runs on every request with an account JWT, on the event loop: a short
# per-account cache keeps it from taking a database round trip each time.
_cache: dict[str, tuple[float, Optional[str]]] = {}   # user_id -> (expires monotonic, sessions_valid_after; None = no account)
_cache_lock = threading.Lock()


def _cache_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("SESSION_CACHE_SECONDS", "5")))
    except ValueError:
        return 5.0


def _cache_drop(user_id: Optional[str] = None) -> None:
    with _cache_lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


def _valid_after(user_id: str) -> Optional[str]:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(user_id)
        if hit and hit[0] > now:
            return hit[1]
    acct = by_user_id(user_id)
    after = acct["sessions_valid_after"] if acct else None
    ttl = _cache_ttl()
    if ttl:
        with _cache_lock:
            if len(_cache) > 5000:
                _cache.clear()
            _cache[user_id] = (now + ttl, after)
    return after


def session_valid(claims: dict) -> bool:
    """Called by auth.decode_token for account JWTs (claim acct)."""
    after = _valid_after(claims.get("sub", ""))
    if after is None:
        return False
    if not after:
        return True
    iat = claims.get("iat")
    return isinstance(iat, (int, float)) and iat >= _epoch(after)


UNVERIFIED_KEEP_DAYS = 7


def purge_expired() -> None:
    now = db.now_ts()
    old = _in(timedelta(days=-UNVERIFIED_KEEP_DAYS))
    # Only when verification is required: with it off, an account is never expected to verify.
    stale = [r["user_id"] for r in db.query("SELECT user_id FROM accounts WHERE email_verified = 0 AND created_at < ?", (old,))] \
        if require_verified() else []
    for uid in stale:           # never verified in a week: the name and email are free again
        db.execute("DELETE FROM auth_tokens WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM accounts WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
        _cache_drop(uid)
    db.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (now,))
    db.execute("DELETE FROM refresh_tokens WHERE expires_at < ?", (now,))
