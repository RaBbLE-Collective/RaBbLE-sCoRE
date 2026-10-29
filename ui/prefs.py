"""The Observer's UI settings: model, Crew on/off, permission mode. Saved between
launches so `/model opus` or `/crew on` sticks. One shared object: the app edits it,
the backend factory reads it when a session (re)starts.

File: $RABBLE_UI_PREFS or ~/.config/rabble/ui.json. No path = in memory only (tests).
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

MODES = ("default", "acceptEdits", "plan", "auto")
# picker entries: (label, value). Aliases track the newest model of a tier; None = Claude Code's default.
MODELS = (
    ("default", None),
    ("opus", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
    ("fable", "claude-fable-5-1"),
)


def default_path() -> Path:
    env = os.environ.get("RABBLE_UI_PREFS")
    return Path(env).expanduser() if env else Path.home() / ".config" / "rabble" / "ui.json"


@dataclass
class Settings:
    model: str | None = None
    crew: bool = False  # off by default: organs cost a full context each
    mode: str = "default"
    trust: str | None = None  # None = earned from entropy (orchestra/trust.py)
    park: str = "auto"  # auto | ask | off: what happens when context passes park.PARK_AT
    park_continue: bool = False  # the fresh session carries on from the handoff instead of waiting for Mark
    path: Path | None = None
    # not saved: the running session's Entropy, handed to the next backend when the engine
    # restarts on the same session (/crew), so a restart can't reset the score
    carry: object | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or default_path()
        s = cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        if isinstance(data, dict):
            model = data.get("model")
            s.model = model if isinstance(model, str) and model else None
            s.crew = data.get("crew") is True
            s.trust = data.get("trust") if data.get("trust") in ("strict", "standard", "trusted") else None
            s.mode = data.get("mode") if data.get("mode") in MODES else "default"
            s.park = data.get("park") if data.get("park") in ("auto", "ask", "off") else "auto"
            s.park_continue = data.get("park_continue") is True
        return s

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"model": self.model, "crew": self.crew, "mode": self.mode, "trust": self.trust,
                                         "park": self.park, "park_continue": self.park_continue}, indent=2) + "\n",
                                 encoding="utf-8")
        except OSError:
            pass  # a read-only home must not stop the session


def resolve_model(arg: str) -> str | None:
    """`/model` argument → model value. `default` clears the override; anything else
    (alias or full id) goes through to Claude Code, which rejects what it doesn't know."""
    arg = arg.strip()
    for label, value in MODELS:
        if arg.lower() == label:
            return value
    return arg
