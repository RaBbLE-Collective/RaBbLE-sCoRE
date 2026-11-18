"""Gaze parity with the Rust shell (`mod gaze` in tui/rabble-tui/src/app.rs): the pure logic in ui/gaze.py, then
the app: pointer steers the header mark and /face without a repaint, the entity picks its own focus when the
pointer idles. Cell-aspect parsing parity too.
  PYTHONPATH=. ~/.local/share/RaBbLE/rabble-ui/bin/python -P -m pytest tests/test_ui_gaze.py"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("textual")
pytest.importorskip("claude_agent_sdk")

from orchestra import events as ev  # noqa: E402
from orchestra.backend import ReplayBackend  # noqa: E402
from ui import face as F  # noqa: E402
from ui import gaze as G  # noqa: E402
from ui.app import RabbleApp  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "orchestra"


# ---- pure logic (mirrors the Rust unit tests) --------------------------------------------------------------

def test_choose_priority():
    base = dict(cursor=(3.0, 4.0), newest=(9.0, 8.0))
    assert G.choose(G.Scene(streaming=True, newest=(9.0, 8.0))) == (G.TEXT, (9.0, 8.0))
    assert G.choose(G.Scene(newest=(9.0, 8.0))) is None, "a finished reply is not interesting"
    assert G.choose(G.Scene(card=True, newest=(9.0, 8.0))) == (G.CARD, (9.0, 8.0))
    assert G.choose(G.Scene(typing=True, card=True, streaming=True, **base)) == (G.TYPING, (3.0, 4.0))
    assert G.choose(G.Scene(modal=(1.0, 1.0), typing=True, card=True, streaming=True, **base)) == (G.MODAL, (1.0, 1.0))
    assert G.choose(G.Scene(streaming=True)) is None, "nothing measured, nothing to look at"


def test_hysteresis():
    s, t0 = G.Steer(), 100.0
    assert s.pick((G.TEXT, (1.0, 1.0)), t0) == (1.0, 1.0), "first focus is taken at once"
    assert s.pick((G.TEXT, (2.0, 2.0)), t0 + .1) == (2.0, 2.0), "same focus tracks its target"
    assert s.pick((G.TYPING, (5.0, 5.0)), t0 + .2) == (2.0, 2.0), "a new focus waits"
    assert s.pick(None, t0 + .3) == (2.0, 2.0), "and so does losing interest"
    assert s.pick((G.TYPING, (5.0, 5.0)), t0 + G.ATTN_HOLD + .001) == (5.0, 5.0)
    assert s.pick(None, t0 + G.ATTN_HOLD * 3) is None, "after the hold it lets go"


def test_aim_is_relative_and_clamped():
    assert G.aim((50.0, 10.0), (50.0, 10.0)) == (0.0, 0.0)
    x, y = G.aim((10.0, 30.0), (50.0, 10.0))
    assert x == -1.0 and 0 < y <= 1.0
    x, _ = G.aim((60.0, 10.0), (50.0, 10.0))
    assert 0 < x < 1


def test_constants_match_rust():
    src = (Path(__file__).parent.parent / "tui/rabble-tui/src/app.rs").read_text()
    for name, val in (("EVERY", G.EVERY), ("POINTER_ACTIVE", G.POINTER_ACTIVE), ("POINTER_TTL", G.POINTER_TTL),
                      ("ATTN_EVERY", G.ATTN_EVERY), ("ATTN_HOLD", G.ATTN_HOLD), ("ATTN_TTL", G.ATTN_TTL),
                      ("TYPING_HOLD", G.TYPING_HOLD), ("CARD_HOLD", G.CARD_HOLD)):
        assert f"pub const {name}: Duration = Duration::from_millis({round(val * 1000)});" in src, name
    assert f"pub const RANGE_COLS: f64 = {G.RANGE_COLS:.1f};" in src
    assert f"pub const RANGE_ROWS: f64 = {G.RANGE_ROWS:.1f};" in src


def test_step_pointer_then_lapse_then_saccades():
    s, t0 = G.Steer(), 10.0
    s.pointer = ((2, 30), t0)
    aimed, ttl = s.step(t0 + .01, lambda: G.Scene(), [(50.0, 3.0), (50.0, 20.0)])
    assert ttl == G.POINTER_TTL and aimed[0][0] < 0 and aimed[0][1] > 0
    assert s.step(t0 + .05, lambda: pytest.fail("too soon"), [None]) is None, "cadence"
    aimed, ttl = s.step(t0 + G.POINTER_ACTIVE + .5, lambda: G.Scene(), [(50.0, 3.0), None])
    assert aimed == [None, None] and ttl == G.ATTN_TTL, "idle and nothing interesting: saccades"


def test_step_rechooses_only_every_attn_every():
    s, asked = G.Steer(), []
    scene = lambda: asked.append(1) or G.Scene(typing=True, cursor=(5.0, 5.0))   # noqa: E731
    s.step(0.0, scene, [(0.0, 0.0)])
    s.step(0.2, scene, [(0.0, 0.0)])
    s.step(0.4, scene, [(0.0, 0.0)])
    assert len(asked) == 1
    s.step(0.6, scene, [(0.0, 0.0)])
    assert len(asked) == 2


# ---- cell aspect -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [("2.3", 2.3), ("+2.3", 2.3), ("2.", 2.0), ("2e0", 2.0),
                                      (" 2.3", None), ("2.3 ", None), ("2_3", None), ("", None), ("inf", None),
                                      ("nan", None), ("9", None), ("0.5", None), ("２.３", None)])
def test_cell_aspect_env_is_strict(monkeypatch, raw, want):
    monkeypatch.setenv("RABBLE_CELL_ASPECT", raw)
    F.reset_cell_aspect()
    monkeypatch.setattr("fcntl.ioctl", lambda *a: (_ for _ in ()).throw(OSError()))   # no terminal: fallback is 2.0
    try:
        assert F.cell_aspect() == (2.0 if want is None else want)
    finally:
        F.reset_cell_aspect()


def test_cell_aspect_is_cached_until_reset(monkeypatch):
    monkeypatch.setenv("RABBLE_CELL_ASPECT", "2.3")
    F.reset_cell_aspect()
    assert F.cell_aspect() == 2.3
    monkeypatch.setenv("RABBLE_CELL_ASPECT", "1.5")
    assert F.cell_aspect() == 2.3, "cached"
    F.reset_cell_aspect()
    assert F.cell_aspect() == 1.5
    F.reset_cell_aspect()


# ---- the app -----------------------------------------------------------------------------------------------

def make(tmp_path):
    return RabbleApp(lambda resume, cont: ReplayBackend.from_file(FIX / "tool-turn.jsonl"), cwd=str(tmp_path))


async def settle(pilot):
    await pilot.pause(0.2)
    pilot.app._gaze_timer.stop()   # the tests drive the ticks on a virtual clock
    pilot.app._gz.next = pilot.app._gz.attn_next = None


@pytest.mark.asyncio
async def test_pointer_steers_both_faces_without_a_repaint_and_is_not_typing(tmp_path):
    app = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        w = F.FaceWidget("full")
        await app.transcript.mount(w)
        app.transcript.transient.append(w)
        await settle(pilot)
        app._gz.typed_at = None
        calls = []
        orig = app.refresh
        app.refresh = lambda *a, **k: calls.append(1) or orig(*a, **k)
        await pilot.hover(app.composer, offset=(2, 0))
        assert app._gz.pointer is not None and app._gz.typed_at is None, "motion is remembered, not typing"
        assert not calls, "motion is not a repaint"
        t0 = app._gz.pointer[1]
        app._gaze_tick(t0 + .01)
        mini, full = app._gz.aimed[0], app._gz.aimed[-1]
        assert mini is not None and mini[1] > 0, f"the pointer is below the mark: {mini}"
        assert full is not None, "a /face view follows too"
        assert app.face.life.external is not None
        app._gaze_tick(t0 + G.POINTER_ACTIVE + .5)
        assert all(a is None for a in app._gz.aimed), "pointer idled, nothing else interesting: saccades"
        assert app.face.life.external is None


@pytest.mark.asyncio
async def test_entity_follows_streaming_text_in_real_loop_order_past_attn_hold(tmp_path):
    app = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        base = 1_000_000.0
        app._gaze_tick(base)
        assert app._gz.aimed[0] is None, "idle: random saccades"
        await app._handle(ev.TextDelta("rabble", "hello there"))
        await pilot.pause(0.1)   # first paint of the new text
        app._gaze_tick(base + 1.0)
        assert app._gz.aimed[0] is not None and app._gz.aimed[0][1] > 0, f"looks down at the newest line: {app._gz.aimed}"
        # more text streams in; the page is mid-layout each time the tick runs (tick first, then settle)
        for k in range(1, 5):
            await app._handle(ev.TextDelta("rabble", " and more"))
            app._gaze_tick(base + G.ATTN_HOLD + G.ATTN_EVERY * k + k)
            await pilot.pause(0.02)
        assert app._gz.aimed[0] is not None and app._gz.aimed[0][1] > 0, f"still watching past ATTN_HOLD: {app._gz.aimed}"
        # a measurement that fails mid-layout keeps the last good newest line
        good = app._gz.newest
        app._measure_newest = lambda: G.MISS
        await app._handle(ev.TextDelta("rabble", " again"))
        app._gaze_tick(base + 30.0)
        assert app._gz.newest == good and app._gz.aimed[0] is not None, "a stale page never blinds it"
        # reply ends, user types: the composer holds the eyes, then they let go
        await app._handle(ev.TextBlock("rabble", "hello there"))
        t = base + 40.0
        app._gz.typed_at = t + G.ATTN_HOLD
        app._gaze_tick(t + G.ATTN_HOLD + .6)
        assert app._gz.aimed[0] is not None, "typing keeps its interest"
        app._gaze_tick(t + G.TYPING_HOLD + G.ATTN_HOLD * 3)
        assert app._gz.aimed[0] is None, "nothing left to look at"


@pytest.mark.asyncio
async def test_modal_wins_and_a_key_in_the_composer_counts_as_typing(tmp_path):
    app = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot)
        await pilot.press("x")
        assert app._gz.typed_at is not None, "a keystroke reaches the eyes"
        base = 2_000_000.0
        app._gz.typed_at = None
        await app._handle(ev.PermissionAsk(id="1", organ="rabble", tool="Bash", input={"command": "ls"}))
        await settle(pilot)
        assert app._ask_open
        app._gaze_tick(base)
        assert app._gz.aimed[0] is not None, "the modal is looked at"
        assert app._gz.current() is not None
