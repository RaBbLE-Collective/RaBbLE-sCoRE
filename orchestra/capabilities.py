"""What a model can do, by category, before a session degrades it.

The numbers live in capabilities.json (shipped) and ~/.config/rabble/capabilities.json (the
Observer's own, $RABBLE_CAPABILITIES in tests). They are estimates until a `source` says
otherwise; this module only loads and matches them. Entropy (orchestra/entropy.py) subtracts what
a session loses; trust (orchestra/trust.py) reads the result.

Pure data: no SDK, no UI.
"""

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

CATEGORIES = ("general", "coding", "tools", "recall")
TRUSTED_AT, STANDARD_AT = 60, 40   # capability -> trust tier
STRONG_AT, MEDIUM_AT = 65, 40      # general capability -> tier (drops from one to a lower one cost entropy)

_SHIPPED = Path(__file__).with_name("capabilities.json")


_NEW = re.compile(r"(opus|sonnet|haiku|fable)-(\d{1,2})(?:[-.](\d{1,2}))?(?!\d)")        # claude-opus-5-5, claude-opus-4-5-20251101
_OLD = re.compile(r"claude-(\d{1,2})(?:-(\d{1,2}))?-(opus|sonnet|haiku)")   # claude-3-5-sonnet-20241022
_GPT = re.compile(r"gpt-(\d+)(?:\.(\d+))?(?![\d.])")


def parse_model(name: str | None) -> tuple[str | None, str | None]:
    """(family, version) from a model id: claude-opus-5-5 -> (opus, 5.5), claude-opus-4-20250514 -> (opus, 4),
    claude-3-5-sonnet-20241022 -> (sonnet, 3.5), gpt-6.1 -> (gpt, 6.1). (None, None) when it is not a known pattern."""
    n = (name or "").lower().replace("_", "-")
    if m := _NEW.search(n):
        fam, major, minor = m.groups()
    elif m := _OLD.search(n):
        major, minor, fam = m.groups()
    elif m := _GPT.search(n):
        major, minor = m.groups()
        fam = "gpt"
    else:
        return None, None
    return fam, major + (f".{minor}" if minor is not None else "")


@dataclass(frozen=True)
class Capability:
    key: str
    general: int
    coding: int
    tools: int
    recall: int
    local: bool = False
    source: str = "estimate"
    family: str | None = None
    version: str | None = None   # "5.5"; set on a versioned entry, or on a family fallback to say which version was asked for
    exact: bool = False          # True when this very version is in the registry; False = the family's estimate stands in

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
                      raw.get("local") is True, str(raw.get("source", "estimate")),
                      raw.get("family"), None if raw.get("version") is None else str(raw["version"]), raw.get("version") is not None)


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
        # a versioned entry (family + version) is found by parse_model, never by name fragment, so opus 5.5 can't answer for opus 4.6
        self._exact = {(str(m["family"]).lower(), str(m["version"])): _build(m) for m in models if m.get("family") and m.get("version") is not None}
        self._models = [(tuple(str(f).lower() for f in m.get("match", [m.get("key", "")])), _build(m))
                        for m in models if not (m.get("family") and m.get("version") is not None)]
        self._unknown = _build(unknown)
        default = dict(base.get("default", unknown))
        if isinstance(extra.get("default"), dict):
            default = _entry(extra["default"], default)
        self._default = _build(default)

    def lookup(self, model: str | None) -> Capability:
        name = (model or "").lower()
        if name in ("", "default"):
            return self._default   # Claude Code's own pick: unknown until the session reports it
        family, version = parse_model(name)
        if (family, version) in self._exact:
            return self._exact[(family, version)]
        for fragments, cap in self._models:
            if any(f and f in name for f in fragments):
                # the family's estimate stands in for a version the registry doesn't list: say which was asked for
                return replace(cap, version=version, exact=False) if version and cap.family is None and family == cap.key else cap
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
