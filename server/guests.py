"""
Per-device guest accounts and their shared daily turn budget.

A guest is a real account (tier "guest", handle guest-xxxxxx) issued by a trusted caller holding
GUEST_ISSUE_SECRET (POST /api/v1/users/guest). S347: guests are for TESTING only, not a product path. A Pocket
pairs to a real member account (a device key, devices.py); the beam refuses guest keys and the shared
BEAM_DEVICE_TOKEN unless BEAM_ALLOW_GUEST_POCKETS=true (default false; beam._authorize). The public Pocket
flasher never mints guests: only the owner's own flash script does, with the secret from his own environment.

Guests are pinned to Groq and kept out of sessions and workflows; all guests together get GUEST_DAILY_TURNS
(default 400) model turns per UTC day on the beam and /chat. A refused guest turn is written to the account
event log (guest.limit).

The public WEB demo guest (shared id "guest", auth.DEMO_MODE) is not a guest account and does not use this module.
"""

import os
import secrets
from datetime import datetime, timezone

import account_events
import db
import groqmeter
from fastapi import HTTPException

import users as user_store
from users import UserProfile

TIER = "guest"
BACKEND = "hosted_groq"


def is_guest(user: dict) -> bool:
    return user.get("tier") == TIER


def issue_secret() -> str:
    return os.getenv("GUEST_ISSUE_SECRET", "")


def daily_limit() -> int:
    return int(os.getenv("GUEST_DAILY_TURNS", "400"))


def create_guest(label: str = "") -> tuple[UserProfile, str]:
    """A new guest account. Returns the profile and its API key (the only time it is shown)."""
    taken = user_store.list_handles()
    while True:
        handle = f"guest-{secrets.token_hex(3)}"
        if handle not in taken:
            break
    api_key = f"rbbl_{secrets.token_urlsafe(32)}"
    profile = UserProfile(
        id=secrets.token_urlsafe(12),
        handle=handle,
        display_name=(label.strip()[:40] or "Guest"),
        join_date=datetime.now(timezone.utc).isoformat(),
        intention="",
        llm_backend=BACKEND,
        tier=TIER,
        api_key=api_key,
    )
    user_store.create_user(profile)
    return profile, api_key


def per_guest_limit() -> int:
    return int(os.getenv("GUEST_PER_GUEST_DAILY_TURNS", "100"))


def issue_limit() -> int:
    return int(os.getenv("GUEST_ISSUE_PER_HOUR", "30"))


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def take_turn(user_id: str) -> bool:
    """Spend one turn: from this guest's own daily cap and the pool all guests share.
    False when either is gone (and nothing is spent)."""
    day = _day()
    db.execute("INSERT INTO guest_turns_by_user (day, user_id, n) VALUES (?, ?, 0) "
               "ON CONFLICT(day, user_id) DO NOTHING", (day, user_id))
    if db.execute("UPDATE guest_turns_by_user SET n = n + 1 WHERE day = ? AND user_id = ? AND n < ?",
                  (day, user_id, per_guest_limit())) != 1:
        return False
    db.execute("INSERT INTO guest_turns (day, n) VALUES (?, 0) ON CONFLICT(day) DO NOTHING", (day,))
    if db.execute("UPDATE guest_turns SET n = n + 1 WHERE day = ? AND n < ?", (day, daily_limit())) != 1:
        db.execute("UPDATE guest_turns_by_user SET n = n - 1 WHERE day = ? AND user_id = ?", (day, user_id))
        return False
    return True


def spend(user_id: str) -> str | None:
    """Let a guest turn through, or say why not. Groq's own rate-limit headers come first (guests
    stop when any window is past GUEST_GROQ_CUTOFF so members keep the headroom), then the
    daily pools, which are all there is on a cold start. A refusal is logged as a guest.limit account event."""
    reason = groqmeter.guest_refusal() or (None if take_turn(user_id) else "daily guest allowance used up")
    if reason:
        account_events.record(user_id, "guest.limit", detail=reason)
    return reason


def take_issue_slot() -> bool:
    """One more guest account this UTC hour, up to GUEST_ISSUE_PER_HOUR (counted in the db)."""
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    db.execute("INSERT INTO guest_issue (hour, n) VALUES (?, 0) ON CONFLICT(hour) DO NOTHING", (hour,))
    return db.execute("UPDATE guest_issue SET n = n + 1 WHERE hour = ? AND n < ?", (hour, issue_limit())) == 1


def turns_today() -> int:
    row = db.query_one("SELECT n FROM guest_turns WHERE day = ?", (_day(),))
    return row["n"] if row else 0


def require_turn(user: dict) -> None:
    """HTTP guard for /chat: a guest over the shared daily budget gets a clear 429."""
    if not is_guest(user):
        return
    reason = spend(user["sub"])
    if reason:
        raise HTTPException(429, f"Guest limit reached: {reason}. Guests have a daily allowance, share a pool "
                                 "and pause while the shared Groq quota is busy. Try again later.")
