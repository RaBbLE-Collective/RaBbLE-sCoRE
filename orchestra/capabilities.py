"""What a model can do, by category, before a session degrades it.

The numbers live in capabilities.json (shipped) and ~/.config/rabble/capabilities.json (the
Observer's own, $RABBLE_CAPABILITIES in tests). They are estimates until a `source` says
otherwise; this module only loads and matches them. Entropy (orchestra/entropy.py) subtracts what
a session loses; trust (orchestra/trust.py) reads the result.

Pure data: no SDK, no UI.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

CATEGORIES = ("general", "coding", "tools", "recall")
TRUSTED_AT, STANDARD_AT = 60, 40   # capability -> trust tier
STRONG_AT, MEDIUM_AT = 65, 40      # general capability -> tier (drops from one to a lower one cost entropy)

_SHIPPED = Path(__file__).with_name("capabilities.json")


@dataclass(frozen=True)
class Capability:
    key: str
    general: int
    coding: int
    tools: int
    recall: int
    local: bool = False
    source: str = "estimate"

    def get(self, category: str) -> int:
        return getattr(self, category)

    @property
    def tier(self) -> str:
        return "strong" if self.general >= STRONG_AT else "medium" if self.general >= MEDIUM_AT else "fast"


def user_path() -> Path:
    env = os.environ.get("RABBLE_CAPABILITIES")
    return Path(env).expanduser() if env else Path.home() / ".config" / "rabble" / "capabilities.json"


def _entry(raw: dict, base: dict | None = None) -> dict:
    merged = {**(base or {}), **{k: v for k, v in raw.items() if k != "_about"}}
    return merged


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _build(raw: dict) -> Capability:
    def num(name: str) -> int:
        try:
            return max(0, min(100, int(raw.get(name, raw.get("general", 25)))))
        except (TypeError, ValueError):
            return 25
    return Capability(str(raw.get("key", "unknown")), num("general"), num("coding"), num("tools"), num("recall"),
                      raw.get("local") is True, str(raw.get("source", "estimate")))


class Registry:
    def __init__(self, shipped: Path = _SHIPPED, user: Path | None = None):
        base = _read(shipped)
        models = [dict(m) for m in base.get("models", [])]
        unknown = dict(base.get("unknown", {}))
        extra = _read(user or user_path())
        for m in extra.get("models", []):
            if not isinstance(m, dict) or "key" not in m:
                continue
            for i, old in enumerate(models):
                if old.get("key") == m["key"]:
                    models[i] = _entry(m, old)
                    break
            else:
                models.insert(0, _entry(m))
        if isinstance(extra.get("unknown"), dict):
            unknown = _entry(extra["unknown"], unknown)
        self._models = [(tuple(str(f).lower() for f in m.get("match", [m.get("key", "")])), _build(m)) for m in models]
        self._unknown = _build(unknown)
        default = dict(base.get("default", unknown))
        if isinstance(extra.get("default"), dict):
            default = _entry(extra["default"], default)
        self._default = _build(default)

    def lookup(self, model: str | None) -> Capability:
        name = (model or "").lower()
        if name in ("", "default"):
            return self._default   # Claude Code's own pick: unknown until the session reports it
        for fragments, cap in self._models:
            if any(f and f in name for f in fragments):
                return cap
        return self._unknown


_registry: Registry | None = None


def lookup(model: str | None) -> Capability:
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry.lookup(model)


def reload() -> None:
    """Re-read the files (tests, or after the Observer edits their own)."""
    global _registry
    _registry = None


def trust_for_capability(capability: float) -> str:
    return "trusted" if capability >= TRUSTED_AT else "standard" if capability >= STANDARD_AT else "strict"
