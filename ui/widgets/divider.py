"""One-row separator between the header and the conversation: the palette's cyan and magenta,
flowing. The gradient bounces cyan -> magenta -> cyan across the width and drifts slowly, so the
line moves without drawing the eye (a few frames a second, one segment per run of equal colour)."""

import math
import time

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from ui import theme

FPS = 8
CYCLES = 1.5       # full cyan -> magenta -> cyan sweeps across the width
SPEED = 0.12       # sweeps per second the pattern drifts
GLYPH = "━"        # heavy horizontal: centred in the row, drawn by the terminal itself (VS Code included)


def _rgb(hex_):
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Divider(Widget):
    DEFAULT_CSS = "Divider { height: 1; width: 1fr; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        pal = theme.load_palette()
        self._a, self._b = _rgb(pal["cyan"]), _rgb(pal["magenta"])
        self._t0 = time.monotonic()
        self._phase = 0.0

    def on_mount(self) -> None:
        self.set_interval(1 / FPS, self._tick)

    def _tick(self) -> None:
        self._phase = (time.monotonic() - self._t0) * SPEED
        self.refresh()

    def colour(self, x: int, width: int, phase: float) -> tuple[int, int, int]:
        """Colour of column x: a cosine bounce between the two poles, so no hard seam anywhere."""
        w = 0.5 - 0.5 * math.cos(math.tau * (x / max(width, 1) * CYCLES - phase))
        return tuple(round(a + (b - a) * w) for a, b in zip(self._a, self._b))

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if y != 0 or width <= 0:
            return Strip.blank(width, self.rich_style)
        bg = self.rich_style.bgcolor
        segs, run, last = [], 0, None
        for x in range(width):
            c = self.colour(x, width, self._phase)
            if c != last and run:
                segs.append(Segment(GLYPH * run, Style(color=Color.from_rgb(*last), bgcolor=bg)))
                run = 0
            run += 1
            last = c
        segs.append(Segment(GLYPH * run, Style(color=Color.from_rgb(*last), bgcolor=bg)))
        return Strip(segs)
