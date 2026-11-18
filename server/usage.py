"""
Usage metering and per-account limits (S347 rulings: every user and interaction is logged and tracked;
limits and metering are per ACCOUNT; the Architect/admin is unrestricted but still metered).

record()  one usage_events row per metered call (never raises: a failing database is logged and skipped).
check()   before a model or speech call: HTTP 429 with a plain reason when the account is over a limit.
summary() totals by kind and by day, plus the account's limits and what is left.

What is counted, per UTC day and UTC month, status < 400 only (a failed call costs the account nothing):
  turns    rows with kind "turn": a user-facing model exchange (chat, session message, workflow step,
           beam voice/text/translate turn, capture shaping). kind "tool" (the tool-calling pre-pass) is
           recorded for its tokens but is not a turn.
  audio_s  seconds of kind "stt" (what was transcribed) plus kind "tts" (what was spoken).

Limits (a number; -1 = unlimited; the four fields are daily_turns, monthly_turns, daily_audio_s, monthly_audio_s):
  1. the account's own row in account_limits, field by field (NULL = fall through),
  2. else its tier default: built-ins below, overridden by USAGE_DEFAULTS (JSON, {"free": {"daily_turns": 50}})
     and then by per-tier variables USAGE_<TIER>_<FIELD> (USAGE_FREE_DAILY_TURNS=50). Read on every call.
The tier is read live from the users row (a Pocket device key carries its owner's tier, capped below admin
for access, but its metering follows the owner, so an admin's Pocket is unrestricted too).
The shared public demo guest (id "guest") and per-device guest accounts (tier guest) are recorded but left
unlimited here: guests.py and rate_limit.py cap them as before.

Known looseness: the check happens before the call and the row is written after it, so a burst of concurrent
calls can overshoot a limit by a few turns (the per-minute rate limiter bounds the burst).
"""

import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import account_events
import db

logger = logging.getLogger("rabble.usage")

UNLIMITED = -1
FIELDS = ("daily_turns", "monthly_turns", "daily_audio_s", "monthly_audio_s")
AUDIO_KINDS = {"stt", "tts", "audio"}

_FREE = {"daily_turns": 100, "monthly_turns": 2000, "daily_audio_s": 600, "monthly_audio_s": 10800}
_PRO = {"daily_turns": 500, "monthly_turns": 10000, "daily_audio_s": 3600, "monthly_audio_s": 72000}
_ENTITY = {"daily_turns": 1000, "monthly_turns": 20000, "daily_audio_s": 7200, "monthly_audio_s": 144000}
_OPEN = {f: UNLIMITED for f in FIELDS}

BUILTIN_DEFAULTS = {
    "guest": _OPEN,         # recorded only: guests.py and rate_limit.py cap guests
    "free": _FREE,
    "collective": _PRO,     # legacy invited members
    "pro": _PRO,
    "entity": _ENTITY,
    "admin": _OPEN,         # unrestricted, still metered
}

_last_hit: dict[str, float] = {}     # one usage.limit event per account per minute, so a hammering client cannot flood the log
HIT_EVERY = 60.0


# ── limits ───────────────────────────────────────────────────────────────────

def _int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= UNLIMITED else None


def tier_defaults(tier: str) -> dict:
    """The default limits for a tier (unknown tiers get free's), with the environment applied."""
    tier = (tier or "free").lower()
    if tier not in BUILTIN_DEFAULTS:
        tier = "free"
    out = dict(BUILTIN_DEFAULTS[tier])
    raw = os.getenv("USAGE_DEFAULTS", "").strip()
    if raw:
        try:
            tiers = json.loads(raw)
            over = tiers.get(tier) if isinstance(tiers, dict) else None
            for f in FIELDS:
                if isinstance(over, dict) and _int(over.get(f)) is not None:
                    out[f] = _int(over[f])
        except (ValueError, AttributeError):
            logger.warning("USAGE_DEFAULTS is not valid JSON of {tier: {field: n}}; ignored")
    for f in FIELDS:
        v = _int((os.getenv(f"USAGE_{tier.upper()}_{f.upper()}") or "").strip() or None)
        if v is not None:
            out[f] = v
    return out


def _uid(user) -> str:
    if isinstance(user, str):
        return user
    return str(user.get("sub") or user.get("id") or "")


def tier_of(user) -> str:
    uid = _uid(user)
    if uid == "guest":
        return "guest"
    row = db.query_one("SELECT tier FROM users WHERE id = ?", (uid,)) if uid else None
    if row and row["tier"]:
        return row["tier"]
    return (user.get("tier") if isinstance(user, dict) else None) or "free"


def limits_for(user_id: str, tier: str) -> dict:
    """{"tier", "limits": {field: n}, "source": {field: "account"|"tier"}, "defaults": {field: n}, ...}."""
    defaults = tier_defaults(tier)
    row = db.query_one("SELECT * FROM account_limits WHERE user_id = ?", (user_id,)) or {}
    limits, source = {}, {}
    for f in FIELDS:
        own = _int(row.get(f))
        limits[f], source[f] = (own, "account") if own is not None else (defaults[f], "tier")
    return {"tier": (tier or "free").lower(), "limits": limits, "source": source, "defaults": defaults,
            "note": row.get("note"), "updated_by": row.get("updated_by"), "updated_at": row.get("updated_at")}


def set_limits(user_id: str, changes: dict, by: str, note: str | None = None) -> dict:
    """Merge `changes` ({field: n | None}; -1 unlimited, None back to the tier default) into the account's row."""
    row = db.query_one("SELECT * FROM account_limits WHERE user_id = ?", (user_id,)) or {}
    merged = {f: (changes[f] if f in changes else row.get(f)) for f in FIELDS}
    note = note if note is not None else row.get("note")
    db.execute(
        "INSERT INTO account_limits (user_id, daily_turns, monthly_turns, daily_audio_s, monthly_audio_s, "
        "updated_by, updated_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET daily_turns = excluded.daily_turns, monthly_turns = excluded.monthly_turns, "
        "daily_audio_s = excluded.daily_audio_s, monthly_audio_s = excluded.monthly_audio_s, "
        "updated_by = excluded.updated_by, updated_at = excluded.updated_at, note = excluded.note",
        (user_id, *(merged[f] for f in FIELDS), by, db.now_ts(), note))
    return merged


# ── counting ─────────────────────────────────────────────────────────────────

def _day_start(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT00:00:00.000000Z")


def _month_start(now: datetime) -> str:
    return now.strftime("%Y-%m-01T00:00:00.000000Z")


def used(user_id: str, now: datetime | None = None) -> dict:
    """What the account has spent this UTC day and month, per limit field."""
    now = now or datetime.now(timezone.utc)
    day = _day_start(now)
    row = db.query_one(
        "SELECT "
        "COALESCE(SUM(CASE WHEN kind = 'turn' AND ts >= ? THEN 1 ELSE 0 END), 0) AS daily_turns, "
        "COALESCE(SUM(CASE WHEN kind = 'turn' THEN 1 ELSE 0 END), 0) AS monthly_turns, "
        "COALESCE(SUM(CASE WHEN kind IN ('stt', 'tts') AND ts >= ? THEN COALESCE(audio_s, 0) ELSE 0 END), 0) AS daily_audio_s, "
        "COALESCE(SUM(CASE WHEN kind IN ('stt', 'tts') THEN COALESCE(audio_s, 0) ELSE 0 END), 0) AS monthly_audio_s "
        "FROM usage_events WHERE user_id = ? AND ts >= ? AND COALESCE(status, 200) < 400",
        (day, day, user_id, _month_start(now))) or {}
    return {f: (int(row.get(f) or 0) if "turns" in f else round(float(row.get(f) or 0), 1)) for f in FIELDS}


def remaining(limits: dict, spent: dict) -> dict:
    """Per field: what is left, or None when the field is unlimited."""
    return {f: (None if limits[f] == UNLIMITED else max(0, limits[f] - spent[f])) for f in FIELDS}


def _retry_after(field: str, now: datetime) -> int:
    if field.startswith("daily"):
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - now).total_seconds()))


def refusal(user, kind: str, amount: float = 0.0) -> tuple[str, int] | None:
    """(plain reason, retry-after seconds) when the account is over a limit for this kind of call, else None."""
    uid = _uid(user)
    info = limits_for(uid, tier_of(user))
    audio = kind in AUDIO_KINDS
    fields = [f for f in FIELDS if ("audio" in f) == audio]
    if all(info["limits"][f] == UNLIMITED for f in fields):
        return None
    now = datetime.now(timezone.utc)
    spent = used(uid, now)
    for f in fields:
        lim = info["limits"][f]
        if lim == UNLIMITED:
            continue
        over = (spent[f] + amount > lim) if (audio and amount) else (spent[f] >= lim)
        if over:
            daily = f.startswith("daily")
            unit = "seconds of speech" if audio else "turns"
            who = "set for this account" if info["source"][f] == "account" else "plan default"
            return (f"Usage limit reached: {lim} {unit} per {'day' if daily else 'month'} on the {info['tier']} plan "
                    f"({who}). It resets at 00:00 UTC {'tomorrow' if daily else 'on the 1st'}.",
                    _retry_after(f, now))
    return None


def check(user, kind: str = "turn", amount: float = 0.0) -> None:
    """Raise HTTP 429 (with a plain reason and Retry-After) when the account is over a limit for this kind of
    call. A database failure lets the call through (and is logged): metering is not a security boundary."""
    try:
        hit = refusal(user, kind, amount)
    except Exception as exc:
        logger.warning("usage check skipped: %s", type(exc).__name__)
        return
    if not hit:
        return
    reason, retry = hit
    uid = _uid(user)
    key = f"{uid}:{'audio' if kind in AUDIO_KINDS else 'turn'}"
    now = time.monotonic()
    if now - _last_hit.get(key, -HIT_EVERY) >= HIT_EVERY:
        _last_hit[key] = now
        account_events.record(uid, "usage.limit", detail={"kind": kind, "reason": reason[:160]})
    raise HTTPException(429, reason, headers={"Retry-After": str(retry)})


# ── recording ────────────────────────────────────────────────────────────────

def _n(value):
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def record(user, kind: str, *, workflow: str | None = None, provider: str | None = None, model: str | None = None,
           tokens_in=None, tokens_out=None, audio_s=None, chars=None, status: int | None = 200,
           device: str | None = None, ms=None) -> bool:
    """One usage_events row. `user` is a claims dict or a user id. Never raises."""
    try:
        if device is None and isinstance(user, dict):
            device = user.get("device_id")
        db.execute(
            "INSERT INTO usage_events (id, ts, user_id, kind, workflow, provider, model, tokens_in, tokens_out, "
            "audio_s, chars, status, device, ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(12), db.now_ts(), _uid(user) or "unknown", kind, workflow, provider, model,
             _n(tokens_in), _n(tokens_out), None if audio_s is None else float(audio_s), _n(chars), status,
             str(device)[:40] if device else None, _n(ms)))
        return True
    except Exception as exc:
        logger.warning("usage event %s not recorded: %s", kind, type(exc).__name__)
        return False


# ── reading ──────────────────────────────────────────────────────────────────

def _sums(p: str = "") -> str:
    """The aggregate columns every summary shares; p is a table alias prefix ("e.")."""
    return (f"COUNT(*) AS events, "
            f"COALESCE(SUM(CASE WHEN COALESCE({p}status, 200) < 400 THEN 1 ELSE 0 END), 0) AS ok, "
            f"COALESCE(SUM(CASE WHEN {p}kind = 'turn' AND COALESCE({p}status, 200) < 400 THEN 1 ELSE 0 END), 0) AS turns, "
            f"COALESCE(SUM({p}tokens_in), 0) AS tokens_in, COALESCE(SUM({p}tokens_out), 0) AS tokens_out, "
            f"COALESCE(SUM({p}audio_s), 0) AS audio_s, COALESCE(SUM({p}chars), 0) AS chars")


_COLS = ("events", "ok", "turns", "tokens_in", "tokens_out", "audio_s", "chars")


def _tidy(row: dict) -> dict:
    return {k: (round(float(row.get(k) or 0), 1) if k == "audio_s" else int(row.get(k) or 0)) for k in _COLS}


def _since(days: int) -> tuple[int, str]:
    days = max(1, min(int(days), 366))
    return days, (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def summary(user_id: str, days: int = 30, tier: str | None = None) -> dict:
    days, since = _since(days)
    now = datetime.now(timezone.utc)
    by_kind = {r["kind"]: _tidy(r) for r in db.query(
        f"SELECT kind, {_sums()} FROM usage_events WHERE user_id = ? AND ts >= ? GROUP BY kind", (user_id, since))}
    by_day = [{"day": r["day"], **_tidy(r)} for r in db.query(
        f"SELECT substr(ts, 1, 10) AS day, {_sums()} FROM usage_events WHERE user_id = ? AND ts >= ? "
        "GROUP BY substr(ts, 1, 10) ORDER BY substr(ts, 1, 10)", (user_id, since))]
    total = _tidy(db.query_one(f"SELECT {_sums()} FROM usage_events WHERE user_id = ? AND ts >= ?",
                               (user_id, since)) or {})
    info = limits_for(user_id, tier or tier_of(user_id))
    spent = used(user_id, now)
    return {"user_id": user_id, "days": days, "since": since, "totals": total, "by_kind": by_kind, "by_day": by_day,
            "tier": info["tier"], "limits": info["limits"], "limit_source": info["source"],
            "used": spent, "remaining": remaining(info["limits"], spent)}


def admin_summary(days: int = 30, limit: int = 100) -> dict:
    """Per account totals, busiest first, plus the whole instance."""
    days, since = _since(days)
    limit = max(1, min(int(limit), 500))
    rows = db.query(
        f"SELECT e.user_id AS user_id, u.handle AS handle, u.tier AS tier, {_sums('e.')} "
        "FROM usage_events e LEFT JOIN users u ON u.id = e.user_id WHERE e.ts >= ? "
        "GROUP BY e.user_id, u.handle, u.tier ORDER BY COUNT(*) DESC, e.user_id", (since,))
    accounts = [{"user_id": r["user_id"], "handle": r["handle"], "tier": r["tier"], **_tidy(r)} for r in rows]
    total = _tidy(db.query_one(f"SELECT {_sums()} FROM usage_events WHERE ts >= ?", (since,)) or {})
    top = sorted(accounts, key=lambda a: (-a["turns"], -a["audio_s"], a["user_id"]))[:10]
    return {"days": days, "since": since, "totals": total, "accounts": accounts[:limit], "top_users": top}
