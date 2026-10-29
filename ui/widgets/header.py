"""Header: mini face, gradient wordmark, and the always-visible state: model and mode,
capability by category, entropy and context, provider usage (5h / week), limits, API-equivalent cost."""

import time
from pathlib import Path

from rich.text import Text
from textual.color import Color
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from orchestra import usage


def gradient(text: str, a: str, b: str) -> Text:
    """Bold per-character blend a -> b; colours are palette values, never literals."""
    out = Text()
    ca, cb = Color.parse(a), Color.parse(b)
    n = max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        out.append(ch, style=f"bold {ca.blend(cb, i / n).hex}")
    return out


# the Entropy Tracker doc's glyphs; colours are palette names
ENTROPY_GLYPH = {"STABLE": ("◆", "cyan"), "NOMINAL": ("◆", "cyan"), "ELEVATED": ("▲", "yellow"),
                 "DEGRADED": ("⚠", "magenta"), "UNSTABLE": ("✗", "red")}


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
        self.crew = False
        self.trust = "standard"
        self.entropy: tuple[float, str] | None = None  # (score, band) from the engine
        self.context_pct: float | None = None
        self.park_eta: int | None = None   # turns until the park point at the current fill rate
        self.capability: int | None = None
        self.categories: dict[str, int] = {}
        self.local = False
        self.stability: int | None = None
        self.cost: float | None = None
        self.usage: usage.Usage | None = None   # OS tracker numbers via orchestra.usage
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

    STATE = ("model", "cwd", "mode", "crew", "trust", "entropy", "context_pct", "park_eta", "capability", "categories",
             "local", "stability", "cost", "usage", "limit", "limit_until")

    def export_state(self) -> dict:
        return {k: getattr(self, k) for k in self.STATE}

    def import_state(self, state: dict) -> None:
        for k, v in state.items():
            setattr(self, k, v)
        self.refresh_info()

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

    def _color(self, name: str) -> str:
        return self._pal.get(name, name)

    def _bar(self, pct: float, band: str = "ok", width: int = 10) -> Text:
        """▰▰▱▱: filled share in the band's colour, the rest dim."""
        filled = max(0, min(width, round(pct / 100 * width)))
        color = {"ok": "cyan", "warn": "yellow", "high": "red"}[band]
        t = Text("▰" * filled, style=self._color(color))
        t.append("▱" * (width - filled), style="dim")
        return t

    @staticmethod
    def _cap_band(v: int) -> str:
        return "ok" if v >= 60 else "warn" if v >= 40 else "high"

    def _capability_line(self) -> Text:
        t = Text("capability ", style="dim")
        if not self.categories:
            t.append("--")
            return t
        for name, short in (("general", "gen"), ("coding", "code"), ("tools", "tools"), ("recall", "recall")):
            v = self.categories.get(name)
            if v is None:
                continue
            t.append(f"{short} ", style="dim")
            t.append_text(self._bar(v, self._cap_band(v), 6))
            t.append(f" {v}  ")
        return t

    def _state_line(self) -> Text:
        t = Text()
        if self.entropy:
            score, band = self.entropy
            glyph, color = ENTROPY_GLYPH.get(band, ("◆", "cyan"))
            t.append(f"{glyph} {band.lower()}", style=self._color(color))
            t.append(f" entropy {score:.2f}" if isinstance(score, float) else "", style="dim")
            if self.stability is not None:
                t.append(f" · stab {self.stability}")
            t.append(" · ")
        ctx = "ctx --" if self.context_pct is None else f"ctx {self.context_pct:.0f}%"
        t.append(ctx)
        if self.context_pct is not None:
            t.append(" ")
            t.append_text(self._bar(self.context_pct, "high" if self.context_pct >= 80 else "warn" if self.context_pct >= 60 else "ok", 8))
            if self.park_eta is not None and self.park_eta <= 12:
                t.append(f" park in ~{self.park_eta}" if self.park_eta else " parking", style=self._color("yellow"))
        return t

    def _usage_line(self) -> Text:
        t = Text("usage ", style="dim")
        u = self.usage
        if u is None or (u.five_hour is None and u.week is None):
            t.append("-- (OS tracker not reporting)", style="dim")
            return t
        for label, w in (("5h", u.five_hour), ("wk", u.week)):
            if w is None:
                continue
            t.append(f"{label} ", style="dim")
            t.append_text(self._bar(w.pct, w.band, 8))
            t.append(f" {w.pct:.0f}%")
            t.append(f" ↺{usage.fmt_remaining(w.resets_in())}  ", style="dim")
        if u.stale:
            t.append(f"stale {usage.fmt_remaining(u.age)}", style=self._color("yellow"))
        return t

    def _build(self) -> Text:
        mag, cy = self._color("magenta"), self._color("cyan")
        t = gradient("RaBbLE", mag, cy)
        t.append("  " + short_path(self.cwd) + "\n", style="dim")
        tier = self.trust + (" local" if self.local else "") + ("" if self.capability is None else f" {self.capability}")
        t.append(f"{self.model} · mode {self.mode} · {tier} · crew {'on' if self.crew else 'off'}\n", style="bold")
        t.append_text(self._capability_line())
        t.append("\n")
        t.append_text(self._state_line())
        t.append("\n")
        t.append_text(self._usage_line())
        t.append("\n")
        t.append_text(self._limit_text())
        cost = "--" if self.cost is None else f"${self.cost:.4f}"
        t.append(f" · {cost} API equiv")
        return t

    def refresh_info(self) -> None:
        self._info.update(self._build())
