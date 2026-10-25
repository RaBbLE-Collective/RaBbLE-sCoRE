"""Header: mini face, gradient wordmark, and the always-visible state (model, mode,
cwd, context %, rate limit, API-equivalent cost)."""

import time
from pathlib import Path

from rich.text import Text
from textual.color import Color
from textual.containers import Horizontal, Vertical
from textual.widgets import Static


def gradient(text: str, a: str, b: str) -> Text:
    """Bold per-character blend a -> b; colours are palette values, never literals."""
    out = Text()
    ca, cb = Color.parse(a), Color.parse(b)
    n = max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        out.append(ch, style=f"bold {ca.blend(cb, i / n).hex}")
    return out


def short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


class Header(Horizontal):
    DEFAULT_CSS = """
    Header { height: 6; background: $background; }
    Header #info { width: 1fr; height: 6; padding: 0 1; }
    """

    def __init__(self, face, palette: dict[str, str], *, model: str, cwd: str, mode: str):
        super().__init__()
        self._face = face
        self._pal = palette
        self.model, self.cwd, self.mode = model or "default", cwd, mode
        self.context_pct: float | None = None
        self.cost: float | None = None
        self.limit = "allowed"
        self.limit_until: float | None = None
        self._info = Static(id="info")

    def compose(self):
        yield self._face
        yield self._info

    def on_mount(self) -> None:
        self.refresh_info()

    def set_mood(self, mood: str) -> None:
        setter = getattr(self._face, "set_mood", None)
        if setter:
            setter(mood)

    @property
    def plain(self) -> str:
        return self._build().plain

    def _limit_text(self) -> Text:
        if self.limit == "allowed":
            return Text("limits ok", style="dim")
        until = ""
        if self.limit_until:
            until = " until " + time.strftime("%H:%M", time.localtime(self.limit_until))
        warn = self.limit == "warning"
        color = self._pal.get("yellow" if warn else "red", "yellow" if warn else "red")
        return Text(("limit warning" if warn else "LIMIT REACHED") + until, style=f"bold {color}")

    def _build(self) -> Text:
        mag, cy = self._pal.get("magenta", "magenta"), self._pal.get("cyan", "cyan")
        t = gradient("RaBbLE", mag, cy)
        t.append("\n")
        t.append(f"{self.model} · mode {self.mode}\n", style="bold")
        t.append(short_path(self.cwd) + "\n", style="dim")
        ctx = "ctx --" if self.context_pct is None else f"ctx {self.context_pct:.0f}%"
        t.append(ctx + " · ")
        t.append_text(self._limit_text())
        cost = "--" if self.cost is None else f"${self.cost:.4f}"
        t.append(f" · {cost} API equiv")
        return t

    def refresh_info(self) -> None:
        self._info.update(self._build())
