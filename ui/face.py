"""RaBbLE's face for a truecolor terminal.

Draws the NeBuLA alive entity (orb, two eye/portal pairs, side meshes, the
almond envelope) into terminal cells: half-blocks for filled shapes (eyes,
portals, nodes) and braille dots for lines (wireframe, mesh, envelope).

The face rig and expression table are ported from
RaBbLE-NeBuLA/src/backends/alive-backend.js so both bodies share one creature.
Colours come from RaBbLE-Grimoire/RaBbLE-Agent/RaBbLE-Palette.md at runtime (via ui.theme).

The eyes and portals follow the rig baseline in RaBbLE-NeBuLA/specs/rig/entity-rig.json (tests pin it);
the prototype in RaBbLE-BaBbLE/prototypes/terminal-face/ is the older reference and no longer matches.
FaceWidget is the Textual body.
"""

import asyncio
import functools
import math
import os
import random
import re
import threading
import time

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget

from ui import eyes, theme

# ── face rig (alive-backend.js, face units) ─────────────────────────────────
EYE_RX, EYE_RY, POR_RX, POR_RY, EYE_X, POR_OFF = 43.5, 136, 117, 35, 92, 126
GAZE_X, GAZE_Y = 26, 12   # eye.gaze.x / .y ranges in the rig manifest (face units)
ROUGH_PX = 1.0   # eye travel (px) since the last layer beyond which the layer is painted without edge supersampling
AURA_POLES, AURA_RING = 0.08, 0.16   # halo strength at the eye's edge: faint, a glow and not a shadow
ORB_R = 440  # orb radius in face units, measured from the alive reference shots


def _x(**kw):
    return {"dx": 0, "dy": 0, "rot": 0, "sx": 1, "sy": 1, **kw}


EXPR = {
    "calm": {"eL": _x(), "eR": _x(), "pL": _x(), "pR": _x()},
    "joy": {"eL": _x(dy=-6, sy=.86, sx=1.05, rot=-6), "eR": _x(dy=-6, sy=.86, sx=1.05, rot=6),
            "pL": _x(dy=-20, rot=-8, sx=1.06), "pR": _x(dy=-56, rot=8, sx=1.08)},
    "sad": {"eL": _x(dy=14, sy=.9, rot=7, sx=.96), "eR": _x(dy=18, sy=.9, rot=-7, sx=.96),
            "pL": _x(dy=12, rot=10, sx=.96), "pR": _x(dy=-12, rot=-6, sx=.97)},
    "angry": {"eL": _x(dy=4, sy=.86, rot=-6, sx=1.03), "eR": _x(dy=-4, sy=.86, rot=6, sx=1.03),
              "pL": _x(dy=40, rot=-12, sx=1.08, dx=8), "pR": _x(dy=-40, rot=12, sx=1.08, dx=-8)},
    "listening": {"eL": _x(sy=1.04), "eR": _x(sy=1.04), "pL": _x(dy=252), "pR": _x(dy=-252)},
    "speaking": {"eL": _x(), "eR": _x(), "pL": _x(dy=-44, sx=1.05), "pR": _x(dy=44, sx=1.05)},
}


# Lids for the mini mark, where a 6-7 degree eye tilt is invisible: (depth as a fraction of the eye's
# half-height, slope in px per px; slope > 0 = the inner side sits lower). Angry cuts down toward the
# middle, sad down toward the outside: the brow cue that still reads in a 24x6 cell mark.
LIDS = {"angry": (0.55, 0.9), "sad": (0.45, -0.7)}


# ── palette ─────────────────────────────────────────────────────────────────

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def load_palette():
    """The palette as RGB tuples, from ui.theme (RaBbLE-Palette.md)."""
    return {k: _rgb(v) for k, v in theme.load_palette().items()}


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# ── canvas: half-block pixels + braille dots on one cell grid ──────────────

_BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][col]


def _octant_table():
    """bit pattern (bit i = octant i+1; octants run 1 2 / 3 4 / 5 6 / 7 8) -> glyph.

    Unicode 16's BLOCK OCTANT run skips the 26 patterns that older block glyphs already draw.
    kitty, foot, wezterm and ghostty draw all of these themselves, independent of the font.
    """
    import unicodedata
    table = {}
    for cp in range(0x1CD00, 0x1CDE6):
        m = re.match(r"BLOCK OCTANT-(\d+)$", unicodedata.name(chr(cp), ""))
        if m:
            table[sum(1 << (int(d) - 1) for d in m.group(1))] = chr(cp)
    older = {(): " ", (1,): 0x1CEA8, (2,): 0x1CEAB, (1, 2): 0x1FB82, (1, 3): 0x2598, (2, 4): 0x259D,
             (1, 2, 3, 4): 0x2580, (3, 5): 0x1FBE6, (4, 6): 0x1FBE7, (1, 2, 3, 4, 5, 6): 0x1FB85,
             (7,): 0x1CEA3, (5, 7): 0x2596, (1, 3, 5, 7): 0x258C, (2, 4, 5, 7): 0x259E,
             (1, 2, 3, 4, 5, 7): 0x259B, (8,): 0x1CEA0, (6, 8): 0x2597, (1, 3, 6, 8): 0x259A,
             (2, 4, 6, 8): 0x2590, (1, 2, 3, 4, 6, 8): 0x259C, (7, 8): 0x2582, (5, 6, 7, 8): 0x2584,
             (1, 3, 5, 6, 7, 8): 0x2599, (2, 4, 5, 6, 7, 8): 0x259F, (3, 4, 5, 6, 7, 8): 0x2586,
             tuple(range(1, 9)): 0x2588}
    for octs, ch in older.items():
        table[sum(1 << (o - 1) for o in octs)] = ch if isinstance(ch, str) else chr(ch)
    assert len(table) == 256
    return table


_OCTANT = _octant_table()

# VS Code's terminal (xterm.js) draws Block Elements itself but has no octant glyphs, so the eyes and the mini
# mark would show tofu there. Safe mode keeps to quadrants. RABBLE_GLYPHS=safe|octant overrides the detection.
_glyphs = os.environ.get("RABBLE_GLYPHS", "").lower()
SAFE_GLYPHS = _glyphs == "safe" or (_glyphs != "octant" and os.environ.get("TERM_PROGRAM") == "vscode")


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _mode(cols):
    return max(dict.fromkeys(cols), key=cols.count)


def _avg(cols):
    return tuple(round(sum(c[i] for c in cols) / len(cols)) for i in range(3))


class Canvas:
    """Half-block mode: pixels are (cols, rows*2) and braille dots are 2x that.
    Octant mode: pixels are (cols*2, rows*4) and lines land on the same grid as solid sub-pixels.
    Pixels are square in both."""

    def __init__(self, cols, rows, bg, octant=False, safe=False):
        self.cols, self.rows = cols, rows
        self.octant = octant
        self.safe = safe   # quadrant glyphs only
        self.w, self.h = (cols * 2, rows * 4) if octant else (cols, rows * 2)
        self.bg = [[bg] * cols for _ in range(rows)]
        self.fill = {}  # (px, py) -> rgb
        self.dots = {}  # (dx, dy) -> (rgb, weight)
        self.override = {}  # (row, col) -> (glyph, fg, bg): cells drawn by the fine eye renderer

    def glow(self, fn):
        for r in range(self.rows):
            for c in range(self.cols):
                self.bg[r][c] = fn((c + .5) * self.w / self.cols, (r + .5) * self.h / self.rows, self.bg[r][c])

    def px(self, x, y, color):
        x, y = int(math.floor(x)), int(math.floor(y))
        if 0 <= x < self.w and 0 <= y < self.h:
            self.fill[(x, y)] = color

    def dot(self, x, y, color, weight=1.0):
        """x, y in pixel units; lands on the 2x braille grid (or the pixel grid in octant mode)."""
        k = 1 if self.octant else 2
        dx, dy = int(math.floor(x * k)), int(math.floor(y * k))
        if 0 <= dx < self.w * k and 0 <= dy < self.h * k:
            old = self.dots.get((dx, dy))
            if old is None or weight >= old[1]:
                self.dots[(dx, dy)] = (color, weight)

    def line(self, x0, y0, x1, y1, color, weight=1.0):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) * 2.2))
        for i in range(n + 1):
            t = i / n
            self.dot(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, color, weight)

    def _cell(self, r, c):
        """(char, fg, bg) for one cell."""
        if (r, c) in self.override:
            return self.override[(r, c)]
        bg = self.bg[r][c]
        if self.octant:
            return self._octant_cell(r, c, bg)
        top, bot = self.fill.get((c, r * 2)), self.fill.get((c, r * 2 + 1))
        if top or bot:
            return "▀", top or bg, bot or bg
        bits, best = 0, None
        for yy in range(4):
            for xx in range(2):
                d = self.dots.get((c * 2 + xx, r * 4 + yy))
                if d:
                    bits |= _BRAILLE[yy][xx]
                    if best is None or d[1] > best[1]:
                        best = d
        return (chr(0x2800 + bits) if bits else " "), (best[0] if best else bg), bg

    def _octant_cell(self, r, c, bg):
        """Eight sub-pixels, two colours: split them around the most distant pair."""
        subs = []
        for yy in range(4):
            for xx in range(2):
                key = (c * 2 + xx, r * 4 + yy)
                d = self.dots.get(key)
                subs.append(self.fill.get(key) or (d[0] if d else bg))
        uniq = list(dict.fromkeys(subs))
        if len(uniq) == 1:
            return " ", bg, uniq[0]
        a, b = max(((x, y) for i, x in enumerate(uniq) for y in uniq[i + 1:]), key=lambda pr: _dist(*pr))
        if b == bg:  # keep the cell's glow as the background where it is one of the two
            a, b = b, a
        if a != bg and _dist(a, bg) < _dist(b, bg):
            a, b = b, a
        ins = [_dist(s, b) < _dist(s, a) for s in subs]  # True -> foreground
        # the commonest colour on each side, never an average: averages turn eye tips pastel
        fg = _mode([s for s, i in zip(subs, ins) if i])
        back = _mode([s for s, i in zip(subs, ins) if not i]) if not all(ins) else bg
        if self.safe:   # a quadrant is on when either of its two sub-pixels is: thin lines keep their weight
            for a in (0, 1, 4, 5):
                ins[a] = ins[a + 2] = ins[a] or ins[a + 2]
        bits = sum(1 << i for i, on in enumerate(ins) if on)
        return _OCTANT[bits], fg, back

    def cells(self):
        """(char, fg, bg) grid for previews and for a Textual widget."""
        if self.octant:
            return [[self._cell(r, c) for c in range(self.cols)] for r in range(self.rows)]
        # half-block grid: most cells carry nothing but their glow, so only the cells something touched
        # go through _cell()
        used = set(self.override)
        used.update((y // 2, x) for x, y in self.fill)
        used.update((dy // 4, dx // 2) for dx, dy in self.dots)
        blank, out = {}, []
        for r in range(self.rows):
            row = []
            for c, bg in enumerate(self.bg[r]):
                if (r, c) in used:
                    row.append(self._cell(r, c))
                else:
                    cell = blank.get(bg)
                    if cell is None:
                        cell = blank[bg] = (" ", bg, bg)
                    row.append(cell)
            out.append(row)
        return out

    def ansi(self, base_bg=None):
        """Cells whose background is `base_bg` (the palette's bg) use the terminal's default
        background, so a translucent terminal shows through instead of an opaque block."""
        lines = []
        for row in self.cells():
            out = []
            for ch, f, b in row:
                back = "\x1b[49m" if b == base_bg else f"\x1b[48;2;{b[0]};{b[1]};{b[2]}m"
                out.append(f"\x1b[38;2;{f[0]};{f[1]};{f[2]}m{back}{ch}")
            lines.append("".join(out) + "\x1b[0m")
        return "\n".join(lines)


# ── geometry helpers ────────────────────────────────────────────────────────

def _in_ellipse(x, y, cx, cy, rx, ry, rot):
    c, s = math.cos(rot), math.sin(rot)
    u, v = (x - cx) * c + (y - cy) * s, -(x - cx) * s + (y - cy) * c
    return (u / rx) ** 2 + (v / ry) ** 2


def _in_pill(x, y, cx, cy, rx, ry, rot, n=2.6):
    """Superellipse: the eyes read as blunt pills, not needles, at terminal resolution."""
    c, s = math.cos(rot), math.sin(rot)
    u, v = (x - cx) * c + (y - cy) * s, -(x - cx) * s + (y - cy) * c
    return abs(u / rx) ** n + abs(v / ry) ** n


def _ellipse_pts(cx, cy, rx, ry, rot, a0=0.0, a1=2 * math.pi, n=None):
    n = n or max(24, int((rx + ry) * 4))
    c, s = math.cos(rot), math.sin(rot)
    for i in range(n + 1):
        a = a0 + (a1 - a0) * i / n
        u, v = rx * math.cos(a), ry * math.sin(a)
        yield cx + u * c - v * s, cy + u * s + v * c


def _edge_y(cx, cy, rx, ry, rot, x, lower):
    """y of an ellipse's upper/lower edge at column x (approximate for small rot)."""
    u = (x - cx) / rx
    if abs(u) >= 1:
        return None
    off = ry * math.sqrt(1 - u * u)
    return cy + math.tan(rot) * (x - cx) + (off if lower else -off)


@functools.lru_cache(maxsize=4096)
def _lattice(seed, i):
    return random.Random(seed * 7919 + i).random()


def _noise(seed, x):
    # cheap 1D value noise: smooth enough for lightning strands (lattice values are cached:
    # seeding a Random per sample made the envelope most of the full face's frame time)
    i = math.floor(x)
    f = x - i
    a = _lattice(seed, i)
    b = _lattice(seed, i + 1)
    f = f * f * (3 - 2 * f)
    return a + (b - a) * f - 0.5


# ── the entity ──────────────────────────────────────────────────────────────

# ── provider aura ───────────────────────────────────────────────────────────
# The halo around the eyes takes a faint tint from the model's provider. These are the providers' own brand
# colours (not RaBbLE palette values); each is pulled toward the palette's violet before it is drawn.
# A tuple of colours is a ring, read clockwise from the right (Google's four).
PROVIDER_AURA = {
    "anthropic": ((217, 119, 87),),
    "google": ((66, 133, 244), (52, 168, 83), (251, 188, 5), (234, 67, 53)),   # right, bottom, left, top
    "openai": ((16, 163, 127),),
}
PROVIDER_MUTE = 0.35   # share of palette violet mixed into the provider colour


def provider_of(model, local=False):
    """Provider key for a model id, None for local models and anything unrecognised (= the RaBbLE poles).
    No model named means Claude Code's own default."""
    if local:
        return None
    m = (model or "").lower()
    if not m or any(k in m for k in ("claude", "opus", "sonnet", "haiku", "fable")):
        return "anthropic"
    if any(k in m for k in ("gemini", "gemma")):
        return "google"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "codex")) or "gpt-" in m:
        return "openai"
    return None


def _ring_colour(ring, angle):
    """Colour at `angle` (radians, screen coords: 0 = right, pi/2 = down) around a ring of stops."""
    n = len(ring)
    if n == 1:
        return ring[0]
    pos = (angle % math.tau) / math.tau * n
    k = int(pos) % n
    return mix(ring[k], ring[(k + 1) % n], pos - int(pos))


class Face:
    """One entity instance. `frame(t)` returns a Canvas; `state`/`expr` steer it."""

    def __init__(self, cols, rows, palette=None, detail="full", seed=7, spread=None,
                 eye_n=None, eye_h=None, eye_w=None, aura=None, rim=None, rim_tips=None, safe=None):
        self.cols, self.rows = cols, rows
        self.P = palette or load_palette()
        self.detail = detail  # "full" (orb, mesh, envelope) or "mini" (header mark)
        self.expr = "calm"
        self.pose = None  # explicit transform dict (a blend between expressions); overrides EXPR[expr]
        self.seed = seed
        self.safe = SAFE_GLYPHS if safe is None else safe
        self.octant = detail != "full"  # the mini mark needs the finer grid; the full face reads well in half-blocks
        w, h = (cols * 2, rows * 4) if self.octant else (cols, rows * 2)
        self.cx, self.cy = w / 2, h / 2
        if detail == "full":
            self.R = min(h * 0.36, w * 0.17)
            self.hw = w / 2 - 0.5             # envelope half-width
            self.hh = min(h * 0.49, self.R * 1.38)
            self.k = self.R / ORB_R * 1.45     # face units -> px; face enlarged for legibility
        else:
            self.R = h * 0.48
            self.hw = self.hh = 0
            self.k = (h * (eye_h or 0.33)) / EYE_RY
        self.mins = {"erx": 3, "prx": 5.5, "pry": 2.6} if detail == "full" else {"erx": 3, "prx": 7, "pry": 2.4}
        # 1.0 = the rig baseline (centres 2.1 eye-widths apart); see rig() for how a minimum eye width scales it
        self.spread = spread if spread is not None else 1.0
        # mini look knobs: eye shape exponent (2 = true oval, 3 = blunt pill), min eye half-width in px,
        # background glow strength, and how much of the eye is the coloured rim vs the white core
        # mini defaults (S278, Mark: "more oval like in the face, more distinctive, less BG"): true
        # ovals, wider, solid rim on the long sides only (octant cells hold two colours, so a rim at
        # the tips turns them into arrowheads), and no glow behind them
        full = detail == "full"
        self.eye_n = eye_n or (3 if full else 2)   # the stepped drawing; the fine renderer uses true ellipses
        self.mins["erx"] = (eye_w or self.mins["erx"]) if full else (eye_w or 4)
        self.aura_k = aura if aura is not None else (1.0 if full else 0.0)
        self.rim = rim if rim is not None else (0.75 if full else 1.0)
        self.rim_tips = rim_tips if rim_tips is not None else full
        self.lids = not full
        self.fine_eyes = True   # anti-aliased octant-resolution eyes; False = the original stepped drawing
        self.rim_w = 1.0
        # mini: a tighter glow and sharper edges, so the small mark reads crisp without bleeding into the void
        self.halo, self.edge = (0.6, 0.8) if full else (0.45, 0.55)
        self.block = 1   # eye resolution step (sub-pixels): coarser for a less capable model
        self._eye_key, self._eye_cells = None, {}
        self._eye_rough = False
        self._glow = None
        self._layer = None   # compose(): (halo tints, eye cells) for the current rig
        self.provider = None
        self._env_colours = {}
        self._mesh = self._build_mesh() if detail == "full" else ([], [])

    def set_provider(self, provider):
        if provider != self.provider:
            self.provider, self._glow, self._layer = provider, None, None

    def _aura_ring(self):
        """The provider's colours, muted toward the palette violet; () when the poles stand alone."""
        return tuple(mix(c, self.P["violet"], PROVIDER_MUTE) for c in PROVIDER_AURA.get(self.provider, ()))

    # mesh nodes are fixed per instance; they shimmer, not move
    def _build_mesh(self):
        rng = random.Random(self.seed)
        nodes, edges = [], []
        for side in (-1, 1):
            start = len(nodes)
            count = max(8, int(self.hw / 1.9))
            for _ in range(count):
                for _try in range(30):
                    x = rng.uniform(self.R * 0.75, self.hw * 0.9)
                    lim = self._env_h(x) * 0.78
                    y = rng.uniform(-lim, lim)
                    if all(math.hypot(x * side - nx, y - ny) > 2.0 for nx, ny, _ in nodes[start:]):
                        nodes.append((x * side, y, side))
                        break
            mine = list(range(start, len(nodes)))
            for i in mine:
                xi, yi, _ = nodes[i]
                near = sorted(mine, key=lambda j: math.hypot(nodes[j][0] - xi, nodes[j][1] - yi))[1:6]
                for j in near[: rng.choice((3, 3, 4))] + ([rng.choice(mine)] if rng.random() < 0.25 else []):
                    if (j, i) not in edges:
                        edges.append((i, j))
            # a few long strands out to the envelope tip, like the reference
            for i in rng.sample(mine, min(4, len(mine))):
                edges.append((i, "tip"))
        return nodes, edges

    def _env_h(self, x):
        u = min(1.0, abs(x) / self.hw)
        return self.hh * (1 - u ** 2.2) ** 0.85

    def _side(self, x):
        """Colour across the body: cyan on the left, magenta on the right."""
        P = self.P
        u = (x - self.cx) / max(1.0, self.hw or self.R)
        return mix(P["cyan"], P["magenta"], 0.5 + u * 0.9)

    def rig(self, t, blink=0.0, gaze=(0.0, 0.0), snap=True):
        """The four ellipses in px: dict of name -> (cx, cy, rx, ry, rot). `snap` rounds to whole
        pixels (the old half-block drawing); the fine eye renderer takes them unrounded so motion is smooth."""
        e = self.pose or EXPR[self.expr]
        k = self.k
        gx, gy = gaze
        out = {}
        # rig baseline: eye centres 2.1 eye-widths apart. A minimum eye width (small marks) widens the
        # eye, so the spacing and the portals widen with it instead of crowding
        w0 = max(EYE_RX * k, self.mins["erx"]) / (EYE_RX * k)
        # rig idle motion (full face): the whole entity floats 12*sin(1.05t); the portals bob against each other,
        # top -9*sin(1.57t), bottom +9*sin(1.57t)
        full = self.detail == "full"
        float_y = 12 * math.sin(1.05 * t) if full else 0.0
        bob = 9 * math.sin(1.57 * t) if full else 0.0
        for side, name in ((-1, "L"), (1, "R")):
            ep, pp = e["e" + name], e["p" + name]
            ex = self.cx + side * EYE_X * self.spread * k * w0 + (ep["dx"] + gx * GAZE_X) * k
            ey = self.cy + (ep["dy"] + gy * GAZE_Y + float_y) * k
            erx, ery = EYE_RX * ep["sx"] * k, EYE_RY * ep["sy"] * k
            por_y = self.cy + (side * POR_OFF + pp["dy"] + float_y + side * bob) * k  # left portal above, right below
            # portal width and flatten follow the eye's drawn width (rig baseline: 2.7 widths, 0.3 flat)
            prx = POR_RX * pp["sx"] * k * w0
            pry = max(prx * (POR_RY / POR_RX) * pp["sy"] / pp["sx"], 1.2)
            px_ = self.cx + side * EYE_X * self.spread * k * w0 + pp["dx"] * k   # gaze moves the eyes inside their portals; the portals stay
            # blink: the eye sinks into its portal
            b = blink * 0.93
            anchor = por_y
            ery_b = ery * (1 - b)
            ey_b = ey + (anchor - ey) * b
            m = self.mins
            if snap:
                erx = max(round(erx), m["erx"])  # whole-pixel widths keep both sides of the pill symmetric
                ex, px_ = round(ex), round(px_)
            else:
                erx = max(erx, m["erx"])
            out["e" + name] = (ex, ey_b, erx, max(ery_b, 0.6), math.radians(ep["rot"]))
            out["p" + name] = (px_, por_y, max(prx, m["prx"]), max(pry, m["pry"]), math.radians(pp["rot"]))
        return out

    def _aura_fn(self, rig):
        """aura(x, y, base) -> colour: a halo around each eye, starting at its edge (never inside the orb
        or under the eye), tinted by the model's provider; none = the RaBbLE poles."""
        P, ring = self.P, self._aura_ring()
        strength = (AURA_RING if ring else AURA_POLES) * self.aura_k

        def aura(x, y, base):
            c = base
            for name, pole in (("eL", P["cyan"]), ("eR", P["magenta"])):
                ex, ey, rx, ry, _ = rig[name]
                dx, dy = x - ex, y - ey
                u = math.hypot(dx / rx, dy / ry)          # 1 = the eye's edge
                if u <= 1:
                    continue
                v = math.hypot(dx / (rx * 2.6), dy / (ry * 1.55))   # rig: halo 113x211 around a 43.5x136 eye
                if v >= 1:
                    continue
                col = pole if not ring else mix(pole, _ring_colour(ring, math.atan2(dy, dx)), 0.75)
                c = mix(c, col, strength * (1 - v) ** 1.6 * min(1.0, (u - 1) * 3))
            return c
        return aura

    def _aura_box(self, rig):
        """Cell rows/cols (r0, r1, c0, c1) the halos can reach, half-block grid."""
        x0 = min(rig[n][0] - rig[n][2] * 2.6 for n in ("eL", "eR"))
        x1 = max(rig[n][0] + rig[n][2] * 2.6 for n in ("eL", "eR"))
        y0 = min(rig[n][1] - rig[n][3] * 1.55 for n in ("eL", "eR"))
        y1 = max(rig[n][1] + rig[n][3] * 1.55 for n in ("eL", "eR"))
        return (max(0, int(y0 // 2) - 1), min(self.rows - 1, int(y1 // 2) + 1),
                max(0, int(x0) - 1), min(self.cols - 1, int(x1) + 1))

    def background(self, t):
        """The slow layers (envelope, mesh, orb) as plain cells: no eyes, no aura. The widget refreshes this
        a few times a second and composes it under the fast layer."""
        cv = Canvas(self.cols, self.rows, self.P["bg"], octant=self.octant, safe=self.safe)
        if self.detail == "full":
            self._envelope(cv, t)
            self._mesh_draw(cv, t)
        self._orb(cv, t)
        return cv.cells()

    def layer(self, t, blink=0.0, gaze=(0.0, 0.0), last=None):
        """The fast layer of the full face: each eye's halo tint and the eye/portal cells, for the rig at `t`.
        `last` is the previous layer: while the rig holds still it is returned as is. While the eyes travel
        (more than ROUGH_PX since `last`) the edge supersampling is skipped, and the first still call
        repaints them clean."""
        P = self.P
        rig = self.rig(t, blink, gaze, snap=False)
        key = (tuple(round(v * 2) for name in sorted(rig) for v in rig[name][:4])   # half-pixel steps
               + tuple(round(rig[name][4] * 200) for name in sorted(rig)), self.rim_w, self.block)
        if last is not None and last["key"] == key and not last["rough"]:
            return last
        rough = (last is not None and last["key"] != key
                 and max(abs(rig[n][i] - last["rig"][n][i]) for n in rig for i in range(4)) > ROUGH_PX)
        cv = Canvas(self.cols, self.rows, P["bg"])
        aura = self._aura_fn(rig)
        r0, r1, c0, c1 = self._aura_box(rig)
        tints = {}
        for r in range(r0, r1 + 1):
            row = cv.bg[r]
            for c in range(c0, c1 + 1):
                col = aura(c + .5, (r + .5) * 2, P["bg"])
                if col != P["bg"]:
                    row[c] = col
                    tints[(r, c)] = col
        eye_cells = eyes.paint(cv, rig, P, _OCTANT, fine=2, n=2.0, rim_w=self.rim_w, block=self.block,
                               supersample=not rough, safe=self.safe, halo=self.halo, edge=self.edge)
        return {"key": key, "rig": rig, "rough": rough, "tints": tints, "eyes": eye_cells}

    def assemble(self, base, layer):
        """Cells of the full face: the background `base` with the fast `layer` on top. Cheap (a copy of the
        grid and a few hundred cells), so the widget does it every tick."""
        grid = [row[:] for row in base]
        if layer is None:
            return grid
        bg = self.P["bg"]
        for (r, c), col in layer["tints"].items():
            ch, fg, back = grid[r][c]
            grid[r][c] = (ch, col if ch == " " else fg, col if back == bg else back)
        for (r, c), cell in layer["eyes"].items():
            grid[r][c] = cell
        return grid

    def compose(self, t=0.0, blink=0.0, gaze=(0.0, 0.0), base=None):
        """frame().cells() for the full face from a cached background (layer + assemble in one call)."""
        self._layer = self.layer(t, blink, gaze, self._layer)
        return self.assemble(base if base is not None else self.background(t), self._layer)

    def frame(self, t=0.0, blink=0.0, gaze=(0.0, 0.0)):
        P = self.P
        cv = Canvas(self.cols, self.rows, P["bg"], octant=self.octant, safe=self.safe)
        cx, cy, R = self.cx, self.cy, self.R
        rig = self.rig(t, blink, gaze, snap=not self.fine_eyes)
        # the eye cells and the glow behind them depend only on the rig (1/3 px steps), which holds
        # still between saccades and blinks: reuse both
        lid = LIDS.get(self.expr) if self.lids else None
        key = (tuple(round(v * 3) for name in sorted(rig) for v in rig[name][:4])
               + tuple(round(rig[name][4] * 200) for name in sorted(rig)), lid, self.rim_w, self.block)
        moved = key != self._eye_key

        aura = self._aura_fn(rig)

        if moved or self._glow is None:
            cv.glow(aura)
            self._glow = cv.bg
        else:
            cv.bg = self._glow   # read-only after the glow pass

        if self.detail == "full":
            self._envelope(cv, t)
            self._mesh_draw(cv, t)
        self._orb(cv, t)
        if self.fine_eyes:
            if moved or self._eye_rough:
                # while the eyes travel, skip the 2x2 edge supersampling (it is most of the cost); the
                # first still frame repaints them clean
                rough = moved and self._eye_key is not None   # the first frame paints clean
                self._eye_cells = eyes.paint(cv, rig, self.P, _OCTANT, fine=1 if self.octant else 2, n=2.0,
                                             lids={"L": lid, "R": lid} if lid else None, rim_w=self.rim_w,
                                             block=self.block, supersample=not rough, safe=self.safe, halo=self.halo, edge=self.edge)
                self._eye_rough = rough
                self._eye_key = key
            cv.override.update(self._eye_cells)
        else:
            for name in ("L", "R"):
                self._eye_and_portal(cv, rig, name)
        return cv

    def _envelope(self, cv, t):
        P = self.P
        for strand, (scale, w, seed) in enumerate(((1.0, 1.0, 1), (0.92, 0.8, 2), (0.84, 0.6, 3))):
            prev = {}
            steps = int(self.hw * 2 * 2)
            colours = self._env_colours.setdefault(strand, {})
            for i in range(steps + 1):
                x = -self.hw + 2 * self.hw * i / steps
                jag = _noise(seed, x * 0.45 + t * 1.3) * 1.6 + _noise(seed + 9, x * 2.6 - t * 3.1) * 0.9
                for sgn in (-1, 1):
                    y = sgn * (self._env_h(x) * scale + jag * (self._env_h(x) / max(self.hh, 1)) )
                    X, Y = self.cx + x, self.cy + y
                    col = colours.get(i)
                    if col is None:
                        col = colours[i] = mix(mix(P["text"], self._side(X), 0.2 + 0.3 * strand), P["bg"], 0.15 * strand)
                    if sgn in prev:
                        cv.line(prev[sgn][0], prev[sgn][1], X, Y, col, w)
                    prev[sgn] = (X, Y)

    def _mesh_draw(self, cv, t):
        P = self.P
        nodes, edges = self._mesh
        tip = {-1: (self.cx - self.hw, self.cy), 1: (self.cx + self.hw, self.cy)}
        pos = [(self.cx + x, self.cy + y + math.sin(t * 0.9 + i) * 0.25) for i, (x, y, _) in enumerate(nodes)]
        for a, b in edges:
            x0, y0 = pos[a]
            x1, y1 = tip[nodes[a][2]] if b == "tip" else pos[b]
            side_col = P["cyan"] if nodes[a][2] < 0 else P["magenta"]
            cv.line(x0, y0, x1, y1, mix(side_col, P["bg"], 0.12), 0.5)
        for i, (x, y) in enumerate(pos):
            side_col = P["cyan"] if nodes[i][2] < 0 else P["magenta"]
            twinkle = 0.5 + 0.5 * math.sin(t * 2.3 + i * 1.7)
            cv.px(x, y, mix(side_col, P["text"], 0.25 + 0.5 * twinkle))

    def _orb(self, cv, t):
        P = self.P
        cx, cy, R = self.cx, self.cy, self.R
        dim = 0.55 if self.detail == "full" else 0.75  # mini: solid octant ring, so keep it quiet
        # outline
        for x, y in _ellipse_pts(cx, cy, R, R, 0):
            cv.dot(x, y, mix(self._side(x), P["bg"], dim * 0.6), 0.7)
        if self.detail != "full":
            return
        # meridians, turning slowly
        for m in range(5):
            a = t * 0.25 + m * math.pi / 5
            rx = abs(math.cos(a)) * R
            if rx < 0.6:
                continue
            for x, y in _ellipse_pts(cx, cy, rx, R, 0, -math.pi / 2, math.pi / 2, None) if math.cos(a) > 0 else \
                    _ellipse_pts(cx, cy, rx, R, 0, math.pi / 2, 3 * math.pi / 2, None):
                cv.dot(x, y, mix(self._side(x), P["bg"], dim), 0.3)
        # parallels
        for lat in (-0.6, -0.3, 0.0, 0.3, 0.6):
            y0 = cy + R * math.sin(lat * 1.2)
            rx = R * math.cos(lat * 1.2)
            for x, y in _ellipse_pts(cx, y0, rx, rx * 0.12, 0, 0, math.pi):
                cv.dot(x, y, mix(self._side(x), P["bg"], dim), 0.3)
        # motes inside
        rng = random.Random(self.seed + 1)
        for i in range(int(R * 1.6)):
            a, r = rng.uniform(0, 2 * math.pi), R * math.sqrt(rng.random()) * 0.95
            drift = t * 0.4 * (0.5 + rng.random())
            x, y = cx + r * math.cos(a + drift), cy + r * math.sin(a + drift) * 0.9
            col = rng.choice((P["violet"], P["cyan"], P["magenta"], P["pink"]))
            cv.dot(x, y, mix(col, P["bg"], 0.35 + 0.3 * rng.random()), 0.2)

    def _eye_and_portal(self, cv, rig, name):
        P = self.P
        col = P["cyan"] if name == "L" else P["magenta"]
        ex, ey, erx, ery, erot = rig["e" + name]
        px_, py, prx, pry, prot = rig["p" + name]
        below = py > ey  # portal below its eye: front rim draws over the eye
        void = mix(P["bg"], (0, 0, 0), 0.5)

        # portal: dark hole inside a one-pixel rim; rim pixels split into back (upper) and front (lower)
        front = []
        x0, x1 = int(px_ - prx - 1), int(px_ + prx + 1)
        y0, y1 = int(py - pry - 1), int(py + pry + 1)
        for yy in range(y0, y1 + 1):
            for xx in range(x0, x1 + 1):
                fx, fy = xx + .5, yy + .5
                if _in_ellipse(fx, fy, px_, py, prx, pry, prot) > 1:
                    continue
                if _in_ellipse(fx, fy, px_, py, max(prx - 1.4, .3), max(pry - 1.2, .3), prot) <= 1:
                    cv.px(xx, yy, void)
                elif fy > py + math.tan(prot) * (fx - px_) and below:
                    front.append((xx, yy))
                else:
                    cv.px(xx, yy, col)

        # the eye: white core, coloured edge; clipped by its portal (and, mini only, by a lid)
        lid = LIDS.get(self.expr) if self.lids else None
        inner = 1 if name == "L" else -1  # +x points toward the middle for the left eye
        ex0, ex1 = int(ex - erx - 2), int(ex + erx + 2)
        ey0, ey1 = int(ey - ery - 2), int(ey + ery + 2)
        for yy in range(ey0, ey1 + 1):
            for xx in range(ex0, ex1 + 1):
                fx, fy = xx + .5, yy + .5
                d = _in_pill(fx, fy, ex, ey, erx, ery, erot, self.eye_n)
                if d > 1:
                    continue
                if below:
                    edge = _edge_y(px_, py, prx, pry, prot, fx, lower=True)
                    if edge is not None and fy > edge:
                        continue
                    if edge is None and fy > py:
                        continue
                else:
                    edge = _edge_y(px_, py, prx, pry, prot, fx, lower=False)
                    if edge is not None and fy < edge:
                        continue
                    if edge is None and fy < py:
                        continue
                if lid:
                    lid_y = ey - ery + lid[0] * ery + lid[1] * (fx - ex) * inner
                    if fy < lid_y:
                        continue
                    if fy < lid_y + 1.5:  # the lid line itself, in the eye's colour
                        cv.px(xx, yy, col)
                        continue
                edge_px = erx > 1.5 and _in_pill(fx, fy, ex, ey, erx - 1, max(ery - (1 if self.rim_tips else 0), 0.5), erot, self.eye_n) > 1
                cv.px(xx, yy, mix(P["text"], col, self.rim) if edge_px else P["text"])

        # front rim (lower half) over the eye, for a portal below it
        for x, y in front:
            cv.px(x, y, col)


# ── behaviour: blinks + gaze, a light port of alive-backend's eye FSM ──────

class Life:
    """Drives blink and gaze over time so the face never sits still."""

    def __init__(self, seed=3):
        self.rng = random.Random(seed)
        self.next_blink, self.blink_t, self.double = 2.5, -1.0, False
        self.gaze, self.target, self.next_sacc = [0.0, 0.0], [0.0, 0.0], 1.0
        self.speed = 1.0  # >1 = quicker saccades (the "working" mood)

    def step(self, t, dt):
        if self.blink_t < 0 and t >= self.next_blink:
            self.blink_t, self.double = 0.0, self.rng.random() < 0.25
            self.next_blink = t + self.rng.uniform(2.5, 6.0)
        blink = 0.0
        if self.blink_t >= 0:
            self.blink_t += dt
            x = self.blink_t
            if x < .16:
                blink = math.sin(math.pi * x / .16)
            elif self.double and .24 <= x < .40:
                blink = math.sin(math.pi * (x - .24) / .16)
            elif x > .45:
                self.blink_t = -1
        if t >= self.next_sacc:
            self.target = [self.rng.uniform(-1, 1), self.rng.uniform(-.8, .8)]
            self.next_sacc = t + self.rng.uniform(0.6, 2.2) / self.speed
        for i in (0, 1):
            self.gaze[i] += (self.target[i] - self.gaze[i]) * min(1, dt * 9)
        return blink, tuple(self.gaze)


# ── Textual widget ──────────────────────────────────────────────────────────

# engine state -> (expression, saccade speed)
MOODS = {
    "idle": ("calm", 1.0),
    "typing": ("listening", 1.0),
    "streaming": ("speaking", 1.0),
    "working": ("calm", 2.5),
    "error": ("sad", 1.0),
    "denied": ("angry", 1.0),
}
DENIED_SECONDS = 2.0
# Entropy has two inputs (Entropy Tracker v0.5) and the entity draws each its own way, tuned by eye:
#   capability (the model, general 0-100)  -> NOISE: grain, drained colour, coarser eyes. Steady, from
#       the first frame: a fresh haiku is grainy but holds together.
#   stability (the session, 0-100)         -> BREAKING: tears, channel splits, dropped rows. Zero while the
#       session is healthy, so a fresh sonnet is clean; a bloated or compacted opus tears.
NOISE_CURVE = ((100, 0.0), (85, 0.0), (70, 0.06), (35, 0.38), (0, 1.0))     # capability -> noise
BREAK_CURVE = ((100, 0.0), (90, 0.0), (75, 0.12), (40, 0.50), (0, 1.0))     # stability  -> breaking
STATIC_CHARS = "░▒▓·:.'"


def _curve(points, x):
    """Piecewise linear through points (x descending), clamped at both ends."""
    if x >= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x >= x1:
            return y0 + (y1 - y0) * (x0 - x) / (x0 - x1)
    return points[-1][1]


def noise_level(capability):
    """The model's general capability -> 0..1 noise/resolution loss."""
    return _curve(NOISE_CURVE, capability)


def break_level(stability):
    """The session's stability -> 0..1 breaking."""
    return _curve(BREAK_CURVE, stability)


def eye_block(noise):
    """Noise -> the eye renderer's resolution step: 1 = fine, 2 coarse, 3 chunky."""
    return 1 + (noise > 0.25) + (noise > 0.6)


def degrade(cells, brk, pal, rng, state, noise=0.0):
    """The entity's signal. `noise` (from capability) is steady: colour drains toward grey, grain eats
    cells, colour steps coarsen. `brk` (from stability) is the breaking: bursts tear rows sideways with
    a magenta/cyan channel split, and at the top rows drop out. `state` carries the running burst
    between frames. Pure: cells in, cells out."""
    if (brk <= 0 and noise <= 0) or not cells:
        return cells
    bg, text, muted = pal["bg"], pal["text"], pal["muted"]
    magenta, cyan = pal["magenta"], pal["cyan"]
    rows, cols = len(cells), len(cells[0])
    out = []
    # 1. signal loss: drain colour toward its own luma, dim toward the void; a noisy model also
    #    loses colour depth (fewer steps per channel)
    drain, dim = noise * 0.7 + brk * 0.3, noise * 0.35 + brk * 0.2
    step = 1 + int(noise * 40)
    memo = state.get("memo")
    if memo is None or memo[0] != (drain, dim, step):   # the same few hundred colours repeat every frame
        memo = state["memo"] = ((drain, dim, step), {})
    seen = memo[1]
    for row in cells:
        new = []
        for ch, fg, back in row:
            d = seen.get(fg)
            if d is None:
                luma = round(0.3 * fg[0] + 0.59 * fg[1] + 0.11 * fg[2])
                d = mix(mix(fg, (luma, luma, luma), drain), bg, dim)
                if step > 1:
                    d = tuple(min(255, round(v / step) * step) for v in d)
                if len(seen) < 4096:
                    seen[fg] = d
            new.append((ch, d, back))
        out.append(new)
    # 2. bursts: a few rows tear sideways and split into magenta/cyan ghosts
    burst = state.get("burst")
    if burst is None and rng.random() < brk * 0.18:
        n = 1 + int(brk * 2.5)
        burst = {"ticks": rng.randint(2, 5), "shift": {}}
        for r in rng.sample(range(rows), min(rows, n)):
            burst["shift"][r] = rng.choice((-1, 1)) * rng.randint(1, 2 + int(brk * 4))
    if burst:
        for r, k in burst["shift"].items():
            row = out[r]
            moved = row[-k:] + row[:-k] if k else row
            ghost = magenta if k > 0 else cyan
            out[r] = [(ch, mix(fg, ghost, 0.55) if ch != " " else fg, back) for ch, fg, back in moved]
        burst["ticks"] -= 1
        state["burst"] = burst if burst["ticks"] > 0 else None
    # 3. dropout: from 0.6 up, a row sometimes loses signal entirely
    if brk > 0.6 and rng.random() < (brk - 0.6) * 0.5:
        r = rng.randrange(rows)
        out[r] = [(" ", fg, back) for _, fg, back in out[r]]
    # 4. grain: noise replaces cells; denser as the model is less capable, a little more when it breaks
    density = noise * 0.12 + brk * 0.08
    if density > 0:
        for r in range(rows):
            for c in range(cols):
                if rng.random() < density:
                    ch, _, back = out[r][c]
                    grey = mix(muted, text, rng.random() * 0.5)
                    out[r][c] = (rng.choice(STATIC_CHARS), mix(grey, bg, rng.random() * 0.6), back)
    return out


FPS = 20       # mini mark
FULL_FPS = 30  # eyes, blink, gaze; the heavy layers (envelope, mesh, orb) refresh at BG_FPS underneath
BG_FPS = 8
SPRING = 8.0  # pose ease rate per second


def blend_pose(a, b, p):
    """Linear blend of two EXPR pose dicts (dx, dy, rot, sx, sy per eL/eR/pL/pR)."""
    return {part: {k: a[part][k] + (b[part][k] - a[part][k]) * p for k in a[part]} for part in a}


class FaceWidget(Widget):
    """RaBbLE's face. "mini" is a fixed 24x6 mark; "full" fills its container."""

    DEFAULT_CSS = """
    FaceWidget { width: 24; height: 6; }
    FaceWidget.-full { width: 1fr; height: 100%; min-height: 12; }
    """

    def __init__(self, detail="mini", *, name=None, id=None, classes=None):
        super().__init__(name=name, id=id, classes=classes)
        self.detail = detail
        if detail == "full":
            self.add_class("-full")
        self._pal = load_palette()
        self._bg = self._pal["bg"]
        self.life = Life()
        self.face = None
        self._cells = []
        self._t0 = time.monotonic()
        self._last = self._t0
        self._from_pose = EXPR["calm"]
        self._from_expr = self._to_expr = "calm"
        self._blend = 1.0
        self._timer = None
        self._revert = None
        self.noise = self.breaking = 0.0
        self._rng = random.Random(11)
        self._glitch = {}
        self._bg_cost = self._eye_cost = 0.0
        self._base, self._bg_busy, self._bg_started = None, False, 0.0   # the cached background layer (full face)
        self._layer, self._eye_busy, self._eye_started = None, False, 0.0   # the latest eye/halo layer
        self._loop = None
        self.provider = None

    def set_signal(self, capability, stability):
        """The two inputs: the model's general capability (noise, resolution) and the session's
        stability (breaking), both 0-100."""
        self.noise, self.breaking = noise_level(capability), break_level(stability)
        if self.face is not None:
            self.face.block = eye_block(self.noise)
        if self.breaking <= 0:
            self._glitch = {}

    def set_provider(self, provider):
        self.provider = provider
        if self.face is not None:
            self.face.set_provider(provider)

    def _make_face(self):
        if self.detail == "full":
            cols, rows = self.size
            if cols < 4 or rows < 2:
                return None
            face = Face(cols, rows, self._pal, detail="full")
        else:
            face = Face(24, 6, self._pal, detail="mini")
        face.set_provider(self.provider)
        return face

    def on_mount(self):
        self._loop = asyncio.get_running_loop()
        self._timer = self.set_interval(1 / (FULL_FPS if self.detail == "full" else FPS), self._tick)

    def on_resize(self, event):
        self.face, self._base, self._layer = None, None, None  # rebuilt on the next tick at the new size

    def on_unmount(self):
        if self._timer:
            self._timer.stop()

    def set_mood(self, mood):
        expr, speed = MOODS.get(mood, MOODS["idle"])
        self.life.speed = speed
        if self._revert:
            self._revert.stop()
            self._revert = None
        if mood == "denied":
            self._revert = self.set_timer(DENIED_SECONDS, lambda: self.set_mood("idle"))
        if expr != self._to_expr:
            self._from_pose = self._pose()
            self._from_expr = self._expr_now()
            self._to_expr, self._blend = expr, 0.0

    def _pose(self):
        return blend_pose(self._from_pose, EXPR[self._to_expr], self._blend)

    def _expr_now(self):
        return self._to_expr if self._blend > 0.5 else self._from_expr

    def _tick(self):
        now = time.monotonic()
        t, dt = now - self._t0, now - self._last
        self._last = now
        self._blend += (1 - self._blend) * min(1, dt * SPRING)
        if self._blend > 0.995:
            self._blend = 1.0
        if self.face is None:
            self.face = self._make_face()
            if self.face is not None:
                self.face.block = eye_block(self.noise)
        if self.face is None:
            self._cells = []
            return
        blink, gaze = self.life.step(t, dt)
        face = self.face
        face.pose = self._pose()
        face.expr = self._expr_now()
        if self.detail != "full":
            raw = face.frame(t, blink, gaze).cells()   # the mini mark is cheap enough for the loop
        else:
            # three rates, none of them on the event loop except the cheap assembly:
            #   background (envelope, mesh, orb)  BG_FPS, its own thread
            #   eyes and their halo               as fast as the paint runs, its own thread
            #   assembly + noise                  every tick, here
            # daemon threads, not Textual workers: they cannot be cancelled before they run (which would leave a
            # busy flag set for good) and they can never keep the process from quitting. A job stuck for 2 s is
            # abandoned (its result is dropped).
            if self._base is None:
                self._base = face.background(t)
            # each job waits long enough that it holds the GIL for at most a fixed share of the time (the
            # background a quarter, the eyes half): on a huge terminal the paint slows down, the loop does not
            if ((not self._bg_busy or now - self._bg_started > 2.0)
                    and now - self._bg_started >= max(1 / BG_FPS, self._bg_cost * 3)):
                self._bg_busy, self._bg_started = True, now
                threading.Thread(target=self._job, args=("bg", face, face.background, (t,)), daemon=True).start()
            if ((not self._eye_busy or now - self._eye_started > 2.0)
                    and now - self._eye_started >= self._eye_cost * 1.0):
                self._eye_busy, self._eye_started = True, now
                threading.Thread(target=self._job, args=("eyes", face, face.layer, (t, blink, gaze, self._layer)),
                                 daemon=True).start()
            raw = face.assemble(self._base, self._layer)
        cells = degrade(raw, self.breaking, self._pal, self._rng, self._glitch, self.noise)
        old, self._cells = self._cells, cells
        self._refresh_changed(old, cells)

    def _job(self, which, face, fn, args):
        result, t0 = None, time.perf_counter()
        try:
            result = fn(*args)
        except Exception:
            pass   # a mid-resize frame: the next tick rebuilds
        spent = time.perf_counter() - t0
        try:
            self._loop.call_soon_threadsafe(self._job_done, which, face, result, spent)   # never blocks, unlike call_from_thread
        except Exception:
            pass   # the loop is gone: the app is closing

    def _job_done(self, which, face, result, spent=0.0):
        if which == "bg":
            self._bg_busy, self._bg_cost = False, spent
            if result is not None and face is self.face:
                self._base = result
        else:
            self._eye_busy, self._eye_cost = False, spent
            if result is not None and face is self.face:
                self._layer = result

    def _refresh_changed(self, old, new):
        """Repaint only the rows that differ: between background refreshes that is the band around the eyes,
        and every row Textual does not redraw is a row the terminal is not sent."""
        if len(old) != len(new):
            return self.refresh()
        changed = [y for y in range(len(new)) if new[y] != old[y]]
        if not changed:
            return
        if len(changed) > len(new) * 0.6:
            return self.refresh()
        width, regions, start = self.size.width, [], changed[0]
        for a, b in zip(changed, changed[1:] + [None]):
            if b != a + 1:
                regions.append(Region(0, start, width, a - start + 1))
                start = b
        self.refresh(*regions)

    _STYLE_CAP = 20000

    def _style(self, key, base):
        # one Style per (fg, bg, widget background): building Style + Color per run was half the loop's time
        cache = self.__dict__.setdefault("_style_cache", {})
        ck = (key, base.bgcolor)
        st = cache.get(ck)
        if st is None:
            if len(cache) > self._STYLE_CAP:
                cache.clear()
            fg, bg = key
            st = cache[ck] = Style(color=Color.from_rgb(*fg),
                                   bgcolor=base.bgcolor if bg == self._bg else Color.from_rgb(*bg))
        return st

    def render_line(self, y):
        width = self.size.width
        if "NO_COLOR" in os.environ:
            text = "RaBbLE".center(width) if y == self.size.height // 2 else ""
            return Strip([Segment(text.ljust(width), Style())], width)
        # palette-bg cells take the widget's resolved background (the header's), not the
        # terminal default: unset, they'd show as a block that doesn't match the app around it
        base = self.rich_style
        if y >= len(self._cells):
            return Strip.blank(width, base)
        # merge runs of one style into a single segment: far fewer objects to build and emit
        segs, run, last = [], [], None
        for ch, fg, bg in self._cells[y]:
            sk = (fg, bg)
            if sk != last and run:
                segs.append(Segment("".join(run), self._style(last, base)))
                run = []
            run.append(ch)
            last = sk
        if run:
            segs.append(Segment("".join(run), self._style(last, base)))
        return Strip(segs).adjust_cell_length(width)
