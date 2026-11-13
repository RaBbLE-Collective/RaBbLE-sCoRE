import asyncio
import pytest

pytest.importorskip("textual")  # RaBbLE-UI venv only; the server venv skips
from textual.app import App

from ui import face as F


def test_mini_shape():
    cells = F.Face(24, 6, detail="mini").frame(1.0).cells()
    assert len(cells) == 6 and all(len(r) == 24 for r in cells)


@pytest.mark.parametrize("detail,size", [("mini", (24, 6)), ("full", (100, 30)), ("full", (200, 55))])
def test_rig_baseline(detail, size):
    """Idle proportions from RaBbLE-NeBuLA/specs/rig/entity-rig.json `baseline`, in eye widths."""
    r = F.Face(*size, detail=detail).rig(0, snap=False)
    w = 2 * r["eL"][2]
    assert (r["eR"][0] - r["eL"][0]) / w == pytest.approx(2.1, abs=0.05)
    assert 2 * r["pL"][2] / w == pytest.approx(2.7, abs=0.05)
    assert r["pL"][3] / r["pL"][2] == pytest.approx(0.3, abs=0.03)
    assert abs(r["pL"][1] - r["eL"][1]) / r["eL"][3] == pytest.approx(0.93, abs=0.03)
    assert r["pL"][1] < r["eL"][1] < r["pR"][1]   # left portal above its eye, right below
    assert r["eL"][1] == r["eR"][1] and r["eL"][2:4] == r["eR"][2:4]   # level, equal


def test_mood_mapping():
    assert F.MOODS["idle"][0] == "calm" and F.MOODS["typing"][0] == "listening"
    assert F.MOODS["streaming"][0] == "speaking" and F.MOODS["error"][0] == "sad"
    assert F.MOODS["denied"][0] == "angry" and F.MOODS["working"][1] > 1


def test_blend_pose():
    mid = F.blend_pose(F.EXPR["calm"], F.EXPR["listening"], 0.5)
    assert mid["pL"]["dy"] == 126 and F.blend_pose(F.EXPR["calm"], F.EXPR["sad"], 1.0) == F.EXPR["sad"]


class Host(App):
    def __init__(self, detail="mini"):
        super().__init__()
        self.detail = detail

    def compose(self):
        yield F.FaceWidget(self.detail)


def _run(coro):
    return asyncio.run(coro)


def test_widget_renders():
    async def go():
        app = Host()
        async with app.run_test() as pilot:
            w = app.query_one(F.FaceWidget)
            await pilot.pause(0.2)
            assert w.size == (24, 6)
            w.set_mood("error")
            await pilot.pause(0.3)
            assert w._to_expr == "sad" and w._blend > 0.5
            assert all(w.render_line(y).cell_length == 24 for y in range(6))
            assert any(s.text.strip() for s in w.render_line(3))
    _run(go())


def test_full_fills_and_denied_reverts(monkeypatch):
    monkeypatch.setattr(F, "DENIED_SECONDS", 0.2)

    async def go():
        app = Host("full")
        async with app.run_test(size=(80, 30)) as pilot:
            w = app.query_one(F.FaceWidget)
            for _ in range(30):   # the first frame is computed off the loop and arrives a little later
                await pilot.pause(0.1)
                if len(w._cells) == 30:
                    break
            assert w.size == (80, 30) and len(w._cells) == 30
            w.set_mood("denied")
            assert w._to_expr == "angry"
            await pilot.pause(0.5)
            assert w._to_expr == "calm"
    _run(go())


def test_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    async def go():
        app = Host()
        async with app.run_test() as pilot:
            w = app.query_one(F.FaceWidget)
            await pilot.pause(0.1)
            assert "RaBbLE" in "".join(s.text for s in w.render_line(3))
    _run(go())


def test_provider_of():
    assert F.provider_of("claude-opus-5-5") == "anthropic" and F.provider_of(None) == "anthropic"
    assert F.provider_of("gemini-3-pro") == "google" and F.provider_of("gpt-6.1") == "openai"
    assert F.provider_of("claude-opus-5-5", local=True) is None and F.provider_of("llama-4") is None


def test_provider_aura_tints_outside_the_eyes_only():
    plain = F.Face(100, 30, detail="full")
    tinted = F.Face(100, 30, detail="full")
    tinted.set_provider("anthropic")
    a, b = plain.frame(0.0).bg, tinted.frame(0.0).bg
    assert a != b                                                  # the provider tints the halo
    ex, ey, rx, ry, _ = plain.rig(0.0, snap=False)["eL"]
    assert a[int(ey // 2)][int(ex)] == b[int(ey // 2)][int(ex)]   # the eye's own cells carry no aura


def test_safe_glyphs_stay_in_block_elements():
    for detail, size in (("mini", (24, 6)), ("full", (100, 30))):
        cells = F.Face(*size, detail=detail, safe=True).frame(0.5, 0.0, (0.3, 0.1)).cells()
        odd = {ch for row in cells for ch, _, _ in row
               if not (ch == " " or 0x2580 <= ord(ch) <= 0x259F or 0x2800 <= ord(ch) <= 0x28FF)}
        assert not odd, odd


def test_divider_flows_cyan_to_magenta():
    from ui.widgets.divider import Divider
    d = Divider()
    cols = [d.colour(x, 100, 0.0) for x in range(100)]
    assert cols[0] == d._a                                        # starts on the cyan pole
    assert max(cols, key=lambda c: c[0] - c[1]) != cols[0]        # and reaches towards magenta
    assert d.colour(40, 100, 0.0) != d.colour(40, 100, 0.25)      # the phase moves the pattern


@pytest.mark.parametrize("size", [(100, 30), (220, 60)])
def test_compose_matches_frame(size):
    """The widget's layered path (cached background + eye layer) draws what frame() draws."""
    a, b = F.Face(*size, detail="full"), F.Face(*size, detail="full")
    for t, gaze in ((0.0, (0, 0)), (1.3, (0.6, -0.4)), (2.2, (-0.8, 0.3))):
        ref = a.frame(t, 0.0, gaze).cells()
        got = b.compose(t, 0.0, gaze, base=b.background(t))
        diff = sum(1 for r in range(len(ref)) for c in range(len(ref[0])) if ref[r][c] != got[r][c])
        assert diff <= 0.01 * len(ref) * len(ref[0]), (t, diff)


def test_canvas_cells_fast_path_matches_cell():
    f = F.Face(80, 24, detail="full")
    cv = f.frame(0.7, 0.2, (0.3, 0.1))
    assert cv.cells() == [[cv._cell(r, c) for c in range(cv.cols)] for r in range(cv.rows)]
