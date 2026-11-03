"""The three-tier header and the flowing wordmark: the Python twin of widgets/header.rs + the `draw` tiers in app.rs.
  PYTHONPATH=. ~/.local/share/RaBbLE/rabble-ui/bin/python -P -m pytest tests/test_ui_header.py"""

import os
import sys
from pathlib import Path

import pytest
from rich.style import Style

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("textual")
pytest.importorskip("claude_agent_sdk")

from orchestra.backend import ReplayBackend  # noqa: E402
from ui import theme  # noqa: E402
from ui.app import RabbleApp  # noqa: E402
from ui.face import MICRO_COLS  # noqa: E402
from ui.widgets import header as hdr  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "orchestra"


def make(tmp_path):
    return RabbleApp(lambda r, c: ReplayBackend.from_file(sorted(FIX.glob("*.jsonl"))[0]), cwd=str(tmp_path))


def test_constants_match_rust():
    src = (Path(__file__).parent.parent / "tui/rabble-tui/src/widgets/mod.rs").read_text()
    for name, val in (("FACE_COMPACT_SLOT_WIDTH", hdr.FACE_COMPACT_SLOT_WIDTH), ("FACE_COMPACT_ROWS", hdr.FACE_COMPACT_ROWS),
                      ("FACE_SLOT_WIDTH", hdr.FACE_SLOT_WIDTH), ("FACE_ROWS", hdr.FACE_ROWS)):
        assert f"pub const {name}: u16 = {val};" in src or (name == "FACE_COMPACT_SLOT_WIDTH" and val == MICRO_COLS
                                                          and f"pub const {name}: u16 = rabble_face::micro::MICRO_COLS as u16;" in src)
    assert f"FACE_COMPACT_MIN_AREA: (u16, u16) = {hdr.FACE_COMPACT_MIN_AREA};" in src
    assert f"FACE_MIN_AREA: (u16, u16) = {hdr.FACE_MIN_AREA};" in src


@pytest.mark.parametrize("w,h,tier", [(100, 40, "full"), (40, 12, "compact"), (30, 10, "compact"),
                                      (28, 18, "plain"), (80, 9, "plain"), (56, 22, "full"), (56, 21, "compact")])
def test_tier_for(w, h, tier):
    assert hdr.tier_for(w, h) == tier
    assert hdr.tier_for(w, h, has_face=False) == "plain"


def style_of(text, i):
    return Style.parse(str([s.style for s in text.spans if s.start <= i < s.end][-1]))


def mark_colours(text):
    return [style_of(text, i).color.get_truecolor() for i in range(6)]


def test_wordmark_flow_and_fallback(monkeypatch):
    pal = theme.load_palette()
    a, b = hdr.wordmark(pal, 0.0), hdr.wordmark(pal, 0.3)
    assert a.plain == "RaBbLE" and all(style_of(a, i).bold for i in range(6))
    assert mark_colours(a)[0] != mark_colours(a)[5] and mark_colours(a) != mark_colours(b)
    monkeypatch.setenv("NO_COLOR", "1")
    n = hdr.wordmark(pal, 0.0)
    assert n.plain == "RaBbLE" and [str(s.style) for s in n.spans] == ["bold"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size,tier,rows,mark,centre", [((100, 40), "full", 6, (24, 6), (12, 3)),
                                                         ((40, 12), "compact", 2, (7, 2), (3.5, 1.0)),
                                                         ((30, 10), "compact", 2, (7, 2), (3.5, 1.0)),
                                                         ((28, 18), "plain", 2, None, None),
                                                         ((80, 9), "plain", 2, None, None)])
async def test_tiers_in_the_app(tmp_path, size, tier, rows, mark, centre):
    app = make(tmp_path)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.3)
        assert app.header.tier == tier and app.header.size.height == rows
        assert app.divider.region.y == rows and app.transcript.region.y == rows + 1   # nothing below shifts in compact
        if mark is None:
            assert not app.face.display and app.face.region.width == 0 or not app.face.display
            assert app._centre(app.face) is None
        else:
            assert app.face.display and app.face.region.size == mark
            assert app._centre(app.face) == centre
            assert any(ch != " " for row in app.face._cells for ch, _, _ in row)   # drawn, not blank
            assert len(app.face._cells) == mark[1]


@pytest.mark.asyncio
async def test_wordmark_identical_across_tiers_and_reflow(tmp_path, capsys):
    app = make(tmp_path)
    seen = {}
    for size in ((100, 40), (40, 12), (28, 18)):
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.2)
            for phase in (0.0, 0.3):
                app.header.phase = phase
                seen[(app.header.tier, phase)] = mark_colours(app.header._text())
            if app.header.tier == "compact":
                lines = app.header._text().plain.split("\n")
                assert lines[0].startswith("RaBbLE") and all(len(l) <= 40 - 8 for l in lines)   # reflowed to width - 8
        app = make(tmp_path)
    for phase in (0.0, 0.3):
        assert seen[("full", phase)] == seen[("compact", phase)] == seen[("plain", phase)]
    assert seen[("full", 0.0)] != seen[("full", 0.3)]


@pytest.mark.asyncio
async def test_compact_reflow_narrower_than_full_and_flow_follows_divider(tmp_path):
    app = make(tmp_path)
    async with app.run_test(size=(30, 10)) as pilot:
        await pilot.pause(0.3)
        for l in app.header._text().plain.split("\n"):
            assert len(l) <= 30 - 8
        assert app.header.flow in app.divider.follow
        before = app.header.phase
        await pilot.pause(0.4)
        assert app.header.phase != before   # moved on the divider's tick


# ── the micro mark (tui/rabble-face/src/micro.rs tests) ──

from ui.face import micro_cells, micro_pixels  # noqa: E402

PAL = {"bg": (10, 0, 16), "cyan": (0, 245, 255), "magenta": (255, 43, 214)}


def _ascii(blink, gaze):
    return ["".join("#" if p else "." for p in r) for r in micro_pixels(blink, gaze)]


def test_micro_neutral_pins_pixels_and_glyphs():
    assert _ascii(0.0, (0.0, 0.0)) == ["..............", "..#........#..", ".###......###.", ".###......###.", ".###......###.",
                                       ".###......###.", "..#........#..", ".............."]
    g = micro_cells(PAL, 0.0, (0.0, 0.0))
    assert ["".join(c[0] for c in r) for r in g] == ["▗\U0001CDDE   \U0001CDE1▖", "▝\U0001CD17   \U0001CD26▘"]
    assert g[0][1][1:] == (PAL["cyan"], PAL["bg"])
    assert g[0][5][1] == PAL["magenta"]
    assert g[0][3] == (" ", PAL["bg"], PAL["bg"])


def test_micro_gaze_blink_and_gap():
    at = lambda g: _ascii(0.0, g)  # noqa: E731
    for g in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        assert at((0.0, 0.0)) != at(g)
    lit = lambda b: sum(map(sum, micro_pixels(b, (0.0, 0.0))))  # noqa: E731
    assert lit(0.0) > lit(0.4) > lit(0.7) > lit(1.0) > 0
    for gx in (-1.0, 0.0, 1.0):
        for gy in (-1.0, 0.0, 1.0):
            assert all(r[3][0] == " " for r in micro_cells(PAL, 0.0, (gx, gy))), (gx, gy)


def test_micro_safe_is_quadrants_only():
    g = micro_cells(PAL, 0.0, (0.0, 0.0), safe=True)
    assert all(c[0] == " " or "\u2580" <= c[0] <= "\u259f" for r in g for c in r), g
