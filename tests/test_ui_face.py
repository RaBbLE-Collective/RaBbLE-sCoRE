import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("textual")  # RaBbLE-UI venv only; the server venv skips
from textual.app import App

from ui import face as F

PROTO = Path(__file__).resolve().parents[2] / "RaBbLE-BaBbLE/prototypes/terminal-face/face.py"


def test_mini_shape():
    cells = F.Face(24, 6, detail="mini").frame(1.0).cells()
    assert len(cells) == 6 and all(len(r) == 24 for r in cells)


@pytest.mark.skipif(not PROTO.exists(), reason="no prototype")
@pytest.mark.parametrize("detail,size,expr", [("mini", (24, 6), "calm"), ("mini", (24, 6), "angry"),
                                              ("full", (100, 30), "calm")])
def test_port_matches_prototype(detail, size, expr):
    spec = importlib.util.spec_from_file_location("proto_face", PROTO)
    proto = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proto)
    a, b = F.Face(*size, detail=detail), proto.Face(*size, detail=detail)
    a.expr = b.expr = expr
    a.fine_eyes = False   # the port still matches the prototype's stepped drawing; fine eyes are new
    assert a.frame(1.0, 0.3, (0.2, -0.1)).cells() == b.frame(1.0, 0.3, (0.2, -0.1)).cells()


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
            assert all(len(w.render_line(y)) == 24 for y in range(6))
            assert any(s.text.strip() for s in w.render_line(3))
    _run(go())


def test_full_fills_and_denied_reverts(monkeypatch):
    monkeypatch.setattr(F, "DENIED_SECONDS", 0.2)

    async def go():
        app = Host("full")
        async with app.run_test(size=(80, 30)) as pilot:
            w = app.query_one(F.FaceWidget)
            await pilot.pause(0.2)
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
