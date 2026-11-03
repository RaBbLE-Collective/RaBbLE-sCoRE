"""Header: mini face, flowing wordmark, and the always-visible state: model and mode,
capability by category, entropy and context, provider usage (5h / week), limits, API-equivalent cost."""

import os
import time
from pathlib import Path

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from orchestra import usage
from ui.widgets.divider import flow_colour


# the mark's room per tier, mirroring widgets/mod.rs in the Rust shell
FACE_SLOT_WIDTH = 24
FACE_ROWS = 6
FACE_MIN_AREA = (56, 22)
FACE_COMPACT_SLOT_WIDTH = 8
FACE_COMPACT_ROWS = 2
FACE_COMPACT_MIN_AREA = (30, 10)


def tier_for(width: int, height: int, has_face: bool = True) -> str:
    """"full" (6-row mark and info lines), "compact" (8x2 mark, 2-row header) or "plain" (2-row text, no mark)."""
    if has_face and width >= FACE_MIN_AREA[0] and height >= FACE_MIN_AREA[1]:
        return "full"
    if has_face and width >= FACE_COMPACT_MIN_AREA[0] and height >= FACE_COMPACT_MIN_AREA[1]:
        return "compact"
    return "plain"


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def wordmark(pal: dict[str, str], phase: float) -> Text:
    """"RaBbLE", bold, one colour per character on the divider's cyan/magenta flow (`phase` drifts it); every
    header tier draws this. Without colour it is bold only. The Rust twin is `wordmark` in widgets/header.rs."""
    out = Text()
    word = "RaBbLE"
    if "NO_COLOR" in os.environ or "cyan" not in pal or "magenta" not in pal:
        out.append(word, style="bold")
        return out
    a, b = _rgb(pal["cyan"]), _rgb(pal["magenta"])
    for i, ch in enumerate(word):
        r, g, bl = flow_colour(a, b, i, len(word), phase)
        out.append(ch, style=f"bold #{r:02x}{g:02x}{bl:02x}")
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
    Header #info { width: 1fr; height: 6; padding: 0 0 0 1; }
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
        self.tier = "full"
        self.cols = 80          # the pane width, for clipping the two-line tiers
        self.phase = 0.0        # the divider's flow phase, fed by its tick

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
        return self._text().plain

    def _text(self) -> Text:
        return self._build() if self.tier == "full" else self._compact()

    def _compact(self) -> Text:
        """The two lines of the small tiers (Rust `HeaderState::lines`): trailing fields drop, never wrap."""
        slot = {"full": FACE_SLOT_WIDTH, "compact": FACE_COMPACT_SLOT_WIDTH}.get(self.tier, 0)
        width = max(self.cols - (slot + 1 if slot else 0), 1)
        sep = " · "
        mute = "dim"

        def line(first: Text, rest: list[Text]) -> Text:
            parts, used = [first], first.cell_len
            for r in rest:
                if used + len(sep) + r.cell_len > width:
                    break
                parts.append(r)
                used += len(sep) + r.cell_len
            out = Text()
            for i, p in enumerate(parts):
                if i:
                    out.append(sep, style=mute)
                out.append_text(p)
            out.truncate(width)   # a lone long field (the cwd) is cut, never wrapped
            return out

        a_rest = [Text(self.model or "default", style=self._color("violet")),
                  Text(self.mode, style=self._color("cyan"))]
        if self.trust:
            a_rest.append(Text(f"trust {self.trust}", style=mute))
        a_rest.append(Text("crew on" if self.crew else "solo", style=mute))
        b_first = Text(short_path(self.cwd), style=mute)
        b_rest = []
        if self.entropy:
            score, band = self.entropy
            bad = band in ("DEGRADED", "UNSTABLE")
            b_rest.append(Text(f"{band.lower()} {score:.2f}" if isinstance(score, float) else band.lower(),
                               style=self._color("red" if bad else "cyan")))
        if self.cost is not None:
            b_rest.append(Text(f"${self.cost:.2f}", style=mute))
        if self.context_pct is not None:
            b_rest.append(Text(f"ctx {self.context_pct:.0f}%",
                               style=self._color("yellow") if self.context_pct >= 80 else mute))
        if self.limit != "allowed":
            b_rest.append(Text(f"limit {self.limit}", style=self._color("yellow")))
        return Text("\n").join([line(wordmark(self._pal, self.phase), a_rest), line(b_first, b_rest)])

    def set_tier(self, tier: str, cols: int) -> None:
        """Lay the header out for `tier` at pane width `cols` (called on every resize)."""
        self.tier, self.cols = tier, cols
        rows = FACE_ROWS if tier == "full" else 2
        self.styles.height = rows
        self._info.styles.height = rows
        self._info.styles.padding = (0, 0, 0, 1) if tier != "plain" else 0
        fit = getattr(self._face, "fit", None)
        if fit is not None:
            self._face.display = tier != "plain"
            if tier != "plain":
                fit(*((FACE_SLOT_WIDTH, FACE_ROWS) if tier == "full" else (FACE_COMPACT_SLOT_WIDTH, FACE_COMPACT_ROWS)))
        self.refresh_info()

    def flow(self, phase: float) -> None:
        """The divider's tick: the wordmark drifts with the line (only when colour is on)."""
        self.phase = phase
        if "NO_COLOR" not in os.environ:
            self.refresh_info()

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
        t = wordmark(self._pal, self.phase)
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
        self._info.update(self._text())
