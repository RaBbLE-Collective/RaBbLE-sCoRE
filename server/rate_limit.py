import time
import logging
from collections import defaultdict, deque
from fastapi import HTTPException

logger = logging.getLogger("rabble.rate_limit")

# Sliding window counters — key -> deque of monotonic timestamps
_windows: dict[str, deque] = defaultdict(deque)

USER_LIMITS = {
    "free":  {"per_minute": 10,    "per_hour": 100,   "per_day": 500},
    "pro":   {"per_minute": 30,    "per_hour": 500,   "per_day": 5000},
    "admin": {"per_minute": 1000,  "per_hour": 20000, "per_day": 200000},
}

# Strong inference costs ~5x fast; weight throttles quota accordingly
TIER_WEIGHT = {"fast": 1, "medium": 2, "strong": 5}

IP_LIMITS = {"per_minute": 20, "per_hour": 300}


def _evict_and_count(key: str, window_seconds: int) -> int:
    now = time.monotonic()
    dq = _windows[key]
    cutoff = now - window_seconds
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)


def _check(key: str, window_seconds: int, max_requests: int, label: str) -> None:
    count = _evict_and_count(key, window_seconds)
    if count >= max_requests:
        logger.warning("rate_limit exceeded key=%s label=%s count=%d max=%d", key, label, count, max_requests)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({max_requests} requests / {window_seconds}s). Retry shortly.",
            headers={"Retry-After": str(window_seconds)},
        )
    _windows[key].append(time.monotonic())


def apply_user_limit(user_id: str, model_tier: str = "fast", user_tier: str = "free") -> None:
    limits = USER_LIMITS.get(user_tier, USER_LIMITS["free"])
    weight = TIER_WEIGHT.get(model_tier, 1)

    per_min  = max(1, limits["per_minute"] // weight)
    per_hour = max(1, limits["per_hour"]   // weight)
    per_day  = max(1, limits["per_day"]    // weight)

    _check(f"u:{user_id}:m",  60,    per_min,  f"user-{user_id}-minute")
    _check(f"u:{user_id}:h",  3600,  per_hour, f"user-{user_id}-hour")
    _check(f"u:{user_id}:d",  86400, per_day,  f"user-{user_id}-day")


def apply_ip_limit(ip: str) -> None:
    _check(f"ip:{ip}:m",  60,   IP_LIMITS["per_minute"], f"ip-{ip}-minute")
    _check(f"ip:{ip}:h",  3600, IP_LIMITS["per_hour"],   f"ip-{ip}-hour")
