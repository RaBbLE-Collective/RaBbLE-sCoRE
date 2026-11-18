"""Provider quota as the session sees it: Claude's 5-hour and weekly usage.

The numbers come from the RaBbLE-OS tracker (`score-usage-api-poll.py` writes
~/.cache/rabble/llm-usage-latest.json from Anthropic's own usage endpoint). This module only reads
that cache: no daemon, no API call, no cookies. With the tracker not running the fields are None
and `stale` is True; nothing here guesses.

Pure data: no SDK, no UI.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STALE_AFTER = 1200.0   # seconds; the OS bar uses the same cutoff for "observed" numbers
WARN_AT, HIGH_AT = 70.0, 90.0


@dataclass(frozen=True)
class Window:
    pct: float
    resets_at: float | None   # epoch seconds

    def resets_in(self, now: float | None = None) -> float | None:
        return None if self.resets_at is None else max(0.0, self.resets_at - (now or time.time()))

    @property
    def band(self) -> str:
        return "high" if self.pct >= HIGH_AT else "warn" if self.pct >= WARN_AT else "ok"


@dataclass(frozen=True)
class Usage:
    five_hour: Window | None = None
    week: Window | None = None
    age: float | None = None    # seconds since the tracker last wrote

    @property
    def stale(self) -> bool:
        return self.age is None or self.age > STALE_AFTER

    @property
    def pressure(self) -> float:
        """0..1, the worse window; what a router would deprioritise on."""
        pcts = [w.pct for w in (self.five_hour, self.week) if w]
        return max(pcts) / 100 if pcts else 0.0


def cache_path() -> Path:
    env = os.environ.get("RABBLE_USAGE_FILE")
    return Path(env).expanduser() if env else Path.home() / ".cache" / "rabble" / "llm-usage-latest.json"


def _epoch(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


def _window(raw) -> tuple[Window | None, float | None]:
    if not isinstance(raw, dict):
        return None, None
    try:
        pct = max(0.0, float(raw["pct"]))
    except (KeyError, TypeError, ValueError):
        return None, None
    ts = raw.get("ts")
    return Window(pct, _epoch(raw.get("resets_at"))), float(ts) if isinstance(ts, (int, float)) else None


def read(path: Path | None = None, now: float | None = None) -> Usage:
    try:
        data = json.loads((path or cache_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Usage()
    if not isinstance(data, dict):
        return Usage()
    five, t5 = _window(data.get("5h"))
    week, tw = _window(data.get("week"))
    stamps = [t for t in (t5, tw) if t is not None]
    age = (now or time.time()) - max(stamps) if stamps else None
    return Usage(five, week, age)


def fmt_remaining(seconds: float | None) -> str:
    """4h21m, 38m, 2d3h: how long until a window resets."""
    if seconds is None:
        return "?"
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h}h"
