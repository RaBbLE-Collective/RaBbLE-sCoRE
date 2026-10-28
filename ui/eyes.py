"""Fine-resolution eyes and portals for the terminal face.

The NeBuLA entity's eyes are smooth ovals: a near-white sclera, a thin bright rim in the pole colour,
a soft glow, each rising out of (or sinking into) a flat dark portal with its own glowing rim
(RaBbLE-NeBuLA/specs/rig/entity-rig.svg). The half-block drawing this replaces turned them into
thick stepped pills. Here every shape is evaluated analytically at octant resolution (2x4 samples per
cell), edges are anti-aliased by supersampling, and each cell is fitted with the best two colours and
an octant glyph.

Pure functions: rig tuples in, {(row, col): (glyph, fg, bg)} out. `fine` scales the rig's pixel
units to octant sub-pixels (2 for the half-block full face, 1 for the octant mini mark).
"""

import math

TAU = math.pi * 2


def _over(base, top, a):
    if a <= 0:
        return base
    if a >= 1:
        return top
    return (base[0] + (top[0] - base[0]) * a, base[1] + (top[1] - base[1]) * a, base[2] + (top[2] - base[2]) * a)


def _smooth(x):
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return x * x * (3 - 2 * x)


class _Shape:
    """A rotated (super)ellipse with an approximate signed distance (negative inside, in sub-pixels)."""

    __slots__ = ("cx", "cy", "rx", "ry", "n", "c", "s", "reach_x", "reach_y")

    def __init__(self, cx, cy, rx, ry, rot, n=2.0):
        self.cx, self.cy, self.rx, self.ry, self.n = cx, cy, max(rx, 0.3), max(ry, 0.3), n
        self.c, self.s = math.cos(rot), math.sin(rot)
        self.reach_x, self.reach_y = self.rx + 7, self.ry + 7   # beyond this no rim or glow is left

    def sd(self, x, y):
        dx, dy = x - self.cx, y - self.cy
        u = abs(dx * self.c + dy * self.s) + 1e-9
        v = abs(-dx * self.s + dy * self.c) + 1e-9
        n = self.n
        a, b = u / self.rx, v / self.ry
        if n == 2.0:
            f = math.sqrt(a * a + b * b)
            g = math.sqrt((a / self.rx) ** 2 + (b / self.ry) ** 2) / f
        else:
            f = (a ** n + b ** n) ** (1 / n)
            k = f ** (1 - n)
            g = math.sqrt((a ** (n - 1) / self.rx * k) ** 2 + (b ** (n - 1) / self.ry * k) ** 2)
        return (f - 1) / max(g, 1e-6)

    def edge_y(self, x, lower):
        """y of the upper/lower edge at column x, None outside (small-rotation approximation)."""
        u = (x - self.cx) / self.rx
        if abs(u) >= 1:
            return None
        off = self.ry * math.sqrt(1 - u * u)
        t = self.s / self.c if self.c else 0.0
        return self.cy + t * (x - self.cx) + (off if lower else -off)


class _Part:
    """One eye + its portal, with everything the sampler needs."""

    def __init__(self, eye, portal, col, P, fine, n, lid, inner, rim_w):
        ex, ey, erx, ery = (v * fine for v in eye[:4])
        erot = eye[4]
        px, py, prx, pry = (v * fine for v in portal[:4])
        prot = portal[4]
        self.eye = _Shape(ex, ey, erx, ery, erot, n)
        self.portal = _Shape(px, py, prx, pry, prot, 2.0)
        self.below = py > ey
        self.col, self.P = col, P
        self.lid, self.inner = lid, inner
        # rim widths from the SVG ratios (6 units of an 87-wide eye), never thinner than ~1 sub-pixel
        self.eye_rim = max(0.7, erx * 2 * 0.069) * rim_w
        self.por_rim = max(0.9, pry * 2 * 0.085)
        self.box = (min(ex - erx, px - prx), min(ey - ery, py - pry), max(ex + erx, px + prx), max(ey + ery, py + pry))

    def sample(self, x, y, base):
        """Colour at (x, y) in sub-pixels over `base` (the cell's aura-glow background)."""
        P, col = self.P, self.col
        eye, por = self.eye, self.portal
        c = base
        # portal glow, then the back of the portal (void + rim)
        psd = por.sd(x, y) if abs(x - por.cx) < por.reach_x and abs(y - por.cy) < por.reach_y else 99.0
        front = False
        if psd < 6:
            if psd > 0:
                c = _over(c, col, 0.12 * math.exp(-psd / 1.2))
            if psd <= 0:
                c = _over(c, P["void"], _smooth(0.5 - psd))
            core = _smooth(1.0 - (abs(psd) - self.por_rim / 2) / 0.8) if abs(psd) < self.por_rim / 2 + 0.8 else 0.0
            if core > 0:
                if self.below and y > por.cy + por.s / (por.c or 1) * (x - por.cx):
                    front = core
                else:
                    c = _over(c, col, core)
        # the eye, occluded by its portal's near edge
        esd = eye.sd(x, y) if abs(x - eye.cx) < eye.reach_x and abs(y - eye.cy) < eye.reach_y else 99.0
        if esd < 6:
            visible = True
            if self.below:
                edge = por.edge_y(x, True)
                visible = (y <= edge) if edge is not None else (y <= por.cy)
            else:
                edge = por.edge_y(x, False)
                visible = (y >= edge) if edge is not None else (y >= por.cy)
            if visible:
                if self.lid:
                    lid_y = eye.cy - eye.ry + self.lid[0] * eye.ry + self.lid[1] * (x - eye.cx) * self.inner
                    if y < lid_y:
                        visible = False
                if visible:
                    if esd > 0:
                        c = _over(c, col, 0.22 * math.exp(-esd / 1.0))   # halo / outer glow
                    if esd <= 0:
                        depth = min(1.0, -esd / max(eye.rx, 1.0))
                        sclera = _over(P["text"], col, 0.10 * (1 - depth) ** 2)
                        c = _over(c, sclera, _smooth(0.5 - esd))
                    half = self.eye_rim / 2
                    if abs(esd) < half + 0.8:
                        c = _over(c, col, _smooth(1.0 - (abs(esd) - half) / 0.8))
                    if self.lid:
                        d = y - (eye.cy - eye.ry + self.lid[0] * eye.ry + self.lid[1] * (x - eye.cx) * self.inner)
                        if 0 <= d < 1.5:
                            c = _over(c, col, 1 - d / 1.5)
        if front:
            c = _over(c, col, front)
        return c


def _fit(subs, octant):
    """8 sub-pixel colours (row-major 2x4) -> (glyph, fg, bg): two clusters around the most distant pair."""
    best, a, b = -1.0, subs[0], subs[0]
    for i in range(8):
        for j in range(i + 1, 8):
            d = (subs[i][0] - subs[j][0]) ** 2 + (subs[i][1] - subs[j][1]) ** 2 + (subs[i][2] - subs[j][2]) ** 2
            if d > best:
                best, a, b = d, subs[i], subs[j]
    if best < 90:  # nearly flat: no glyph, just the mean
        m = tuple(round(sum(s[k] for s in subs) / 8) for k in range(3))
        return " ", m, m
    ins, fa, fb = 0, [], []
    for i, s in enumerate(subs):
        da = (s[0] - a[0]) ** 2 + (s[1] - a[1]) ** 2 + (s[2] - a[2]) ** 2
        db = (s[0] - b[0]) ** 2 + (s[1] - b[1]) ** 2 + (s[2] - b[2]) ** 2
        if da <= db:
            ins |= 1 << i
            fa.append(s)
        else:
            fb.append(s)
    mean = lambda g: tuple(round(sum(s[k] for s in g) / len(g)) for k in range(3))
    # the brighter cluster draws the glyph (foreground), the darker is the cell background
    ma, mb = mean(fa), mean(fb)
    if sum(ma) < sum(mb):
        ins = ~ins & 0xFF
        ma, mb = mb, ma
    return octant[ins], ma, mb


def paint(canvas, rig, P, octant, fine, n=2.0, lids=None, inner=None, rim_w=1.0, block=1):
    """Both eye/portal pairs as per-cell overrides: {(row, col): (glyph, fg, bg)}. Reads `canvas.bg`
    (the aura glow) but does not write to it, so a caller can cache the result per rig.

    rig: {"eL","eR","pL","pR"} -> (cx, cy, rx, ry, rot) in the face's pixel units.
    lids: {"L": (depth, slope) | None, "R": ...}; inner: {"L": +1, "R": -1}.
    block: resolution step in sub-pixels; above 1 every sub-pixel samples its block's centre, so a model of
    low capability is drawn coarser (and not anti-aliased).
    """
    P = dict(P)
    P.setdefault("void", tuple(max(0, round(v * 0.5)) for v in P["bg"]))
    parts = []
    for side, col in (("L", P["cyan"]), ("R", P["magenta"])):
        parts.append(_Part(rig["e" + side], rig["p" + side], col, P, fine, n,
                           (lids or {}).get(side), (inner or {"L": 1, "R": -1})[side], rim_w))
    x0 = min(p.box[0] for p in parts) - 5
    y0 = min(p.box[1] for p in parts) - 5
    x1 = max(p.box[2] for p in parts) + 5
    y1 = max(p.box[3] for p in parts) + 5
    c0, c1 = max(0, int(x0 // 2)), min(canvas.cols - 1, int(x1 // 2))
    r0, r1 = max(0, int(y0 // 4)), min(canvas.rows - 1, int(y1 // 4))
    # which parts can touch a cell: their padded boxes
    out = {}
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            near = [p for p in parts
                    if p.box[0] - 5 <= c * 2 + 2 and c * 2 <= p.box[2] + 5 and p.box[1] - 5 <= r * 4 + 4 and r * 4 <= p.box[3] + 5]
            if not near:
                continue
            base = canvas.bg[r][c]

            def shade(x, y):
                col = base
                for p in near:
                    col = p.sample(x, y, col)
                return col

            if block > 1:
                snap = lambda v: (v // block) * block + block / 2   # noqa: E731
                subs = [shade(snap(c * 2 + i % 2), snap(r * 4 + i // 2)) for i in range(8)]
                spread = 0
            else:
                subs = [shade(c * 2 + i % 2 + 0.5, r * 4 + i // 2 + 0.5) for i in range(8)]
                spread = max(abs(a[k] - b[k]) for a in subs for b in subs for k in range(3))
            if spread > 40:  # an edge cell: supersample each sub-pixel 2x2
                subs = [tuple(sum(shade(c * 2 + i % 2 + ox, r * 4 + i // 2 + oy)[k] for ox, oy in
                                  ((.25, .25), (.75, .25), (.25, .75), (.75, .75))) / 4 for k in range(3))
                        for i in range(8)]
            ch, fg, bg = _fit(subs, octant)
            if ch == " " and fg == base:
                continue
            out[(r, c)] = (ch, fg, bg)
    return out
