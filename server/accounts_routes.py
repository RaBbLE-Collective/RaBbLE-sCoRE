"""
/api/v1/auth sign-in routes (AC2): signup, login, verify-email, resend-verification, forgot-password,
reset-password, logout, refresh. Env and design notes: accounts.py. Mail: mailer.py.
The legacy register/token/invite routes stay in auth_routes.py untouched.
"""

import hashlib
import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

import accounts
import auth
import users as user_store
import mailer
import rate_limit
from auth_routes import _require_durable

logger = logging.getLogger("rabble.accounts")
router = APIRouter(prefix="/api/v1/auth")

COOKIE = "rabble_refresh"
COOKIE_PATH = "/api/v1/auth"

WAITLIST = ("RaBbLE's rooms are full for now. The doors open a few at a time: "
            "come back soon, and thanks for knocking.")
_FORGOT_REPLY = {"message": "If that email has an account, a reset link is on its way."}
_RESEND_REPLY = {"message": "If that account needs verifying, a fresh link is on its way."}
_BAD_LOGIN = "Invalid username or password."


class SignupRequest(BaseModel):
    username: str = Field(max_length=64)
    email: EmailStr
    password: str = Field(max_length=1024)
    invite_token: str = Field(default="", max_length=128)


class LoginRequest(BaseModel):
    identifier: str = Field(max_length=320, description="username or email")
    password: str = Field(max_length=1024)


class TokenRequest(BaseModel):
    token: str = Field(max_length=256)


class EmailRequest(BaseModel):
    email: EmailStr


class ResetRequest(BaseModel):
    token: str = Field(max_length=256)
    new_password: str = Field(max_length=1024)


def _ip(request: Request) -> str:
    header = os.getenv("AUTH_IP_HEADER", "").strip().lower()
    if header:
        v = request.headers.get(header, "")
        if v:
            return v.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _h(s: str) -> str:
    """Rate-limit keys are logged on a hit: keep typed names and emails out of the log."""
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _limit(key: str, window: int, n: int) -> None:
    rate_limit.apply_limit(key, window, n, key)


def _quota(*rules: tuple[str, int, int]) -> bool:
    """True and counts a hit on every rule if all have room; False (counting nothing) if any is full.
    For mail that is skipped silently, so the reply never changes."""
    if not all(rate_limit.peek(k, w, n) for k, w, n in rules):
        return False
    for k, w, n in rules:
        _limit(k, w, n)
    return True


def _mail_ready() -> bool:
    """False when links are required but no real sender is set: a signup now could never verify."""
    try:
        return not (accounts.require_verified() and isinstance(mailer.get_mailer(), mailer.NullMailer))
    except RuntimeError:
        return False


def _send_link(user_id: str, kind: str) -> None:
    """Runs in the background so the reply (and its timing) is the same whether or not mail goes out."""
    try:
        acct = accounts.by_user_id(user_id)
        if not acct:
            return
        m = mailer.get_mailer()            # before issue_token: a mail failure must not burn the last good link
        raw = accounts.issue_token(user_id, kind)
        path, subject, what = (("/verify-email", "Verify your RaBbLE email", "confirm your email")
                               if kind == "verify" else
                               ("/reset-password", "Reset your RaBbLE password", "choose a new password"))
        link = f"{accounts.link_base()}{path}?token={raw}"
        m.send(acct["email"], subject,
                                 f"Hi {acct['username']},\n\nUse this link to {what}:\n{link}\n\n"
                                 "If you did not ask for this, ignore it.")
    except Exception:
        logger.exception("could not send %s mail user_id=%s", kind, user_id)


def _send_already_registered(email: str) -> None:
    """Sent instead of a verify link when someone signs up with an address that has an account, so the
    signup reply is the same either way and the owner learns of it."""
    try:
        acct = accounts.by_email(email)
        if not acct:
            return
        mailer.get_mailer().send(acct["email"], "Your RaBbLE account",
                                 f"Hi {acct['username']},\n\nSomeone tried to sign up to RaBbLE with this email, "
                                 f"which already has an account.\nSign in as {acct['username']}, or reset your password "
                                 f"at {accounts.link_base()}.\n\nIf this was not you, ignore it.")
    except Exception:
        logger.exception("could not send already-registered mail")


def _session(acct: dict, refresh: str | None = None) -> JSONResponse:
    c = accounts.claims_for(acct)
    token = auth.make_token(c["id"], c["name"], c["tier"], c["handle"], acct=True)
    resp = JSONResponse({
        "token": token, "expires_in": auth.TTL, "user_id": c["id"], "name": c["name"],
        "username": acct["username"], "tier": c["tier"], "email_verified": bool(acct["email_verified"]),
    })
    resp.set_cookie(COOKIE, refresh or accounts.issue_refresh(c["id"]),
                    max_age=int(accounts.refresh_ttl().total_seconds()), path=COOKIE_PATH,
                    httponly=True, secure=True, samesite="lax")
    return resp


def _clear(resp: JSONResponse) -> JSONResponse:
    resp.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, secure=True, samesite="lax")
    return resp


@router.post("/signup", status_code=201)
def signup(body: SignupRequest, request: Request, bg: BackgroundTasks):
    ip = _ip(request)
    _limit(f"auth:signup:ip:{ip}", 3600, 5)
    _require_durable()
    username = body.username.strip()
    email = str(body.email).strip().lower()
    problem = accounts.username_problem(username) or accounts.password_problem(body.password, username, email)
    if problem:
        raise HTTPException(422, problem)
    invite = accounts.live_invite(body.invite_token) if body.invite_token.strip() else None
    if body.invite_token.strip() and not invite:
        raise HTTPException(403, "That invite is invalid, used or expired.")
    if invite is None and accounts.invite_only():
        raise HTTPException(403, "Sign-up is by invitation for now.")
    if invite is None and accounts.count_accounts() >= accounts.account_cap():
        return JSONResponse({"detail": WAITLIST, "waitlist": True}, status_code=403)
    if invite is None and not _mail_ready():
        raise HTTPException(503, "Sign-up is not open yet: email is not set up.")
    # A taken username is answered first and the same whatever the email is, so it cannot be used to probe
    # emails. After that the reply is identical (and costs the same Argon2 work) whether the email is new,
    # has an unverified signup that this one replaces, or belongs to a verified account (which is told by mail).
    existing = accounts.by_email(email)
    own = existing["user_id"] if existing and not existing["email_verified"] else ""
    if accounts.username_taken(username, except_user_id=own):
        raise HTTPException(409, "That username is taken.")
    created = {"username": username, "email_verified": False, "verification_sent": True,
               "message": "Account created. Check your email for the verification link."}
    if invite:      # same reply shape on every invited path, so an invite cannot be used to probe emails
        created = {"username": username, "email_verified": True, "verification_sent": False,
                   "message": "Account created. You can sign in."}
    if existing and existing["email_verified"]:
        accounts.hash_pw(body.password)
        bg.add_task(_send_already_registered, email)
        return JSONResponse(created, status_code=201)
    if existing:
        accounts.drop_unverified(email)        # never verified, so it proves nothing: the new signup replaces it
    try:
        acct = accounts.create_account(username, email, body.password)
    except ValueError as dup:
        if str(dup) == "username":
            raise HTTPException(409, "That username is taken.")
        bg.add_task(_send_already_registered, email)    # lost a race on the email
        return JSONResponse(created, status_code=201)
    accounts.purge_expired()
    if invite:
        if not user_store.consume_invite(invite.token, acct["user_id"]):    # lost a race for the invite
            accounts.drop_unverified(email)
            raise HTTPException(403, "That invite is invalid, used or expired.")
        accounts.mark_verified(acct["user_id"])
    else:
        bg.add_task(_send_link, acct["user_id"], "verify")
    logger.info("account created user_id=%s invited=%s", acct["user_id"], bool(invite))
    return JSONResponse(created, status_code=201)


@router.post("/login")
def login(body: LoginRequest, request: Request):
    ip = _ip(request)
    ident = body.identifier.strip().lower()
    _limit(f"auth:login:ip:{ip}", 300, 20)
    acct = accounts.by_identifier(ident)
    # One budget per account whether the caller typed its username or its email (unknown names count
    # under what was typed). Keyed with the IP so one attacker cannot lock the owner out; a loose
    # per-account cap still stops a distributed guesser.
    who = _h(acct["user_id"] if acct else ident)
    _limit(f"auth:login:acct:{who}:{_h(ip)}", 900, 10)
    _limit(f"auth:login:acct-all:{who}", 3600, 60)
    ok, rehash = accounts.verify_or_dummy(body.password, acct["password_hash"] if acct else None)
    if not (acct and ok):
        raise HTTPException(401, _BAD_LOGIN)
    if rehash:
        accounts.set_password(acct["user_id"], body.password)
    if accounts.require_verified() and not acct["email_verified"]:
        return JSONResponse({"detail": "Verify your email first: check your inbox for the link.",
                             "code": "email_not_verified"}, status_code=403)
    return _session(acct)


@router.post("/verify-email")
def verify_email(body: TokenRequest, request: Request):
    _limit(f"auth:verify:ip:{_ip(request)}", 3600, 30)
    pending = accounts.peek_token(body.token, "verify")
    acct = accounts.by_user_id(pending) if pending else None
    if acct and not acct["email_verified"] and accounts.require_verified() \
            and accounts.count_accounts() >= accounts.account_cap():
        return JSONResponse({"detail": WAITLIST, "waitlist": True}, status_code=403)   # the link stays good
    uid = accounts.consume_token(body.token, "verify")
    if not uid:
        raise HTTPException(400, "That verification link is invalid or has expired.")
    accounts.mark_verified(uid)
    return {"message": "Email verified. You can sign in."}


@router.post("/resend-verification")
def resend_verification(body: EmailRequest, request: Request, bg: BackgroundTasks):
    _limit(f"auth:resend:ip:{_ip(request)}", 3600, 10)
    email = str(body.email).strip().lower()
    acct = accounts.by_email(email)
    if acct and not acct["email_verified"] and _quota(
            (f"auth:resend:acct:{_h(email)}:{_h(_ip(request))}", 3600, 3), (f"auth:resend:acct-all:{_h(email)}", 3600, 10)):
        bg.add_task(_send_link, acct["user_id"], "verify")
    return _RESEND_REPLY


@router.post("/forgot-password")
def forgot_password(body: EmailRequest, request: Request, bg: BackgroundTasks):
    _limit(f"auth:forgot:ip:{_ip(request)}", 3600, 5)
    email = str(body.email).strip().lower()
    acct = accounts.by_email(email)
    # Over the per-account limit the mail is silently skipped: the reply never changes.
    # Per account and IP, so one caller cannot use up the owner's allowance; a looser cap covers all callers.
    if acct and _quota((f"auth:forgot:acct:{_h(email)}:{_h(_ip(request))}", 3600, 3), (f"auth:forgot:acct-all:{_h(email)}", 3600, 10)):
        bg.add_task(_send_link, acct["user_id"], "reset")
    return _FORGOT_REPLY


@router.post("/reset-password")
def reset_password(body: ResetRequest, request: Request):
    _limit(f"auth:reset:ip:{_ip(request)}", 3600, 10)
    _require_durable()
    bad = HTTPException(400, "That reset link is invalid or has expired.")
    uid = accounts.peek_token(body.token, "reset")
    acct = accounts.by_user_id(uid) if uid else None
    if not acct:
        raise bad
    problem = accounts.password_problem(body.new_password, acct["username"], acct["email"])
    if problem:
        raise HTTPException(422, problem)      # token is not spent: they can try again
    if accounts.consume_token(body.token, "reset") != uid:
        raise bad
    accounts.set_password(uid, body.new_password)
    accounts.mark_verified(uid)                # they just proved the mailbox
    accounts.revoke_all(uid)
    return {"message": "Password changed. Sign in again."}


@router.post("/refresh")
def refresh(request: Request):
    raw = request.cookies.get(COOKIE, "")
    try:
        rotated = accounts.rotate_refresh(raw) if raw else None
    except accounts.RefreshRace:
        return JSONResponse({"detail": "Refresh already in progress; try again."}, status_code=409)
    acct = accounts.by_user_id(rotated[0]) if rotated else None
    if not (rotated and acct):
        return _clear(JSONResponse({"detail": "Session expired. Sign in again."}, status_code=401))
    return _session(acct, refresh=rotated[1])


@router.post("/logout")
def logout(request: Request):
    raw = request.cookies.get(COOKIE, "")
    if raw:
        accounts.revoke_presented(raw)
    return _clear(JSONResponse({"ok": True}))
