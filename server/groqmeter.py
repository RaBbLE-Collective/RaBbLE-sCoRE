"""
A rolling record of Groq's own rate-limit headers, per endpoint and model.

Groq answers every call with x-ratelimit-limit-<kind>, x-ratelimit-remaining-<kind> and
x-ratelimit-reset-<kind> (kind is requests, a daily window, or tokens, a per-minute window;
speech endpoints report their own). We keep the latest of each triple. Guests are refused
while ANY live window shows more than GUEST_GROQ_CUTOFF (default 0.5) consumed (per-minute tokens
windows: GUEST_GROQ_MINUTE_CUTOFF, default 0.9), so members
and the owner keep the rest. A window that has passed its reset time counts as empty again.
No data yet (cold start): no cutoff, and the daily guest turn pools in guests.py still apply.

In memory, with a short copy in the db (meta, untracked: not worth a snapshot) so a restart
does not forget what Groq said a minute ago.
"""

import json
import os
import re
import threading
import time

import db

_lock = threading.Lock()
_limits: dict[str, dict] = {}      # "endpoint:model:kind" -> {limit, remaining, reset_at (epoch), seen}
_loaded = False
_saved = 0.0
SAVE_EVERY_S = 30
_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}


def cutoff() -> float:
    return float(os.getenv("GUEST_GROQ_CUTOFF", "0.5"))


def minute_cutoff() -> float:
    """Per-minute token windows refill in seconds and one guest's own talk fills them (the history
    grows each turn), so they get a looser cutoff than the daily windows."""
    return float(os.getenv("GUEST_GROQ_MINUTE_CUTOFF", "0.9"))


def parse_reset(text: str) -> float:
    """Groq's reset hint ("2m59.56s", "7.66s", "1h2m", "250ms") in seconds; 0 when unreadable."""
    return sum(float(n) * _UNIT[u] for n, u in _DUR.findall(text or ""))


def record(endpoint: str, model: str, headers) -> None:
    """Take every x-ratelimit-limit/remaining pair from a Groq response's headers."""
    now = time.time()
    found = {}
    for name in headers.keys():
        m = re.fullmatch(r"x-ratelimit-limit-(\w+)", name.lower())
        if not m:
            continue
        kind = m.group(1)
        try:
            limit = float(headers[name])
            remaining = float(headers[f"x-ratelimit-remaining-{kind}"])
        except (KeyError, ValueError):
            continue
        reset = parse_reset(headers.get(f"x-ratelimit-reset-{kind}", ""))
        found[f"{endpoint}:{model}:{kind}"] = {"limit": limit, "remaining": remaining,
                                               "reset_at": now + reset, "seen": now}
    if not found:
        return
    with _lock:
        _load()
        _limits.update(found)
        _save(now)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = db.meta_get("groq_limits")
        now = time.time()
        for k, v in (json.loads(raw).items() if raw else []):
            if v["reset_at"] > now:
                _limits.setdefault(k, v)
    except Exception:
        pass


def _save(now: float, force: bool = False) -> None:
    global _saved
    if force or now - _saved >= SAVE_EVERY_S:
        _saved = now
        try:
            db.meta_set("groq_limits", json.dumps(_limits))
        except Exception:
            pass


def reset() -> None:
    """Tests: forget everything."""
    global _loaded, _saved
    with _lock:
        _limits.clear()
        _loaded, _saved = False, 0.0


def snapshot() -> list[dict]:
    """The live windows (past their reset time they are gone), with how much of each is used."""
    now = time.time()
    with _lock:
        _load()
        out = []
        for key, v in sorted(_limits.items()):
            if v["reset_at"] <= now or v["limit"] <= 0:
                continue
            endpoint, model, kind = key.split(":", 2)
            out.append({"endpoint": endpoint, "model": model, "kind": kind, "limit": v["limit"],
                        "remaining": v["remaining"], "used": round(1 - v["remaining"] / v["limit"], 3),
                        "resets_in_s": round(v["reset_at"] - now, 1)})
        return out


def guest_refusal() -> str | None:
    """Why guests are turned away right now, or None."""
    for w in snapshot():
        limit = minute_cutoff() if w["kind"] == "tokens" else cutoff()
        if w["used"] > limit:
            return (f"Groq {w['endpoint']} {w['kind']} is {w['used']:.0%} used "
                    f"(guest cutoff {limit:.0%}, resets in {w['resets_in_s']:.0f}s)")
    return None


def state() -> dict:
    return {"cutoff": cutoff(), "refusing_guests": guest_refusal(), "windows": snapshot()}
