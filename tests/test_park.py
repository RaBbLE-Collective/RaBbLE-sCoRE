"""Parking: at 80% context the lead writes a handoff + session log, then a fresh session reads it.
  ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests/test_park.py"""

import asyncio
import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from orchestra import events as ev  # noqa: E402
from orchestra import park  # noqa: E402


def test_handoff_dir_prefers_the_grimoire(tmp_path):
    (tmp_path / "RaBbLE-Grimoire" / "log" / "handoffs").mkdir(parents=True)
    work = tmp_path / "RaBbLE-sCoRE" / "ui"
    work.mkdir(parents=True)
    assert park.handoff_dir(work) == (tmp_path / "RaBbLE-Grimoire" / "log" / "handoffs").resolve()
    assert park.handoff_dir(tmp_path / "RaBbLE-sCoRE").name == "handoffs"


def test_handoff_dir_falls_back_to_the_project(tmp_path):
    d = park.handoff_dir(tmp_path)
    assert d == tmp_path.resolve() / ".rabble" / "handoffs"


def test_handoff_path_names_the_session(tmp_path):
    p = park.handoff_path(tmp_path, "abcdef1234567890", now=0)
    assert p.name.startswith("HANDOFF-parked-") and p.name.endswith("-abcdef12.md")


def test_prompts_carry_the_path_and_rules(tmp_path):
    path = tmp_path / "H.md"
    pp = park.park_prompt(path, 81.4)
    assert str(path) in pp and "81%" in pp and "session log" in pp and "Never push" in pp
    assert "commit point" in pp
    assert "wait for me" in park.fresh_prompt(path, False)
    assert "continue with its 'next' list" in park.fresh_prompt(path, True)
    assert str(path) in park.fresh_prompt(path, False)


def test_check_handoff(tmp_path):
    p = tmp_path / "H.md"
    assert "no file" in park.check_handoff(p, time.time())
    p.write_text("x" * 50)
    assert "bytes" in park.check_handoff(p, time.time() - 5)
    p.write_text("x" * 400)
    assert park.check_handoff(p, time.time() - 5) is None
    old = time.time() - 3600
    os.utime(p, (old, old))
    assert "not written this turn" in park.check_handoff(p, time.time())


def test_bloat_tracks_fill_rate_and_eta():
    b = park.Bloat()
    assert b.reason() is None and b.eta() is None and "no context" in b.summary()
    for tokens, pct in ((20_000, 10.0), (40_000, 20.0), (60_000, 30.0)):
        b.add(tokens, pct)
    assert b.growth() == 10.0 and b.eta() == 5 and b.reason() is None      # 30 + 5*10 = 80
    assert b.reads == 120_000 and "0.12M token-reads over 3 turns" in b.summary()
    b.add(80_000, 40.0); b.add(100_000, 50.0)
    assert b.reason() is None and b.eta() == 3
    b.add(156_000, 78.0)                                                  # a 28% jump: the next could pass 95
    assert "filling" in b.reason() and "95%" in b.reason()
    b.add(160_000, 80.0)
    assert b.reason() == "context 80%" and b.eta() == 0


def test_bloat_forgets_growth_after_a_compaction():
    b = park.Bloat()
    for i in range(1, 8):
        b.add(i * 10_000, i * 10.0)
    b.add(10_000, 10.0)                                                   # compacted
    assert len(b.samples) == 1 and b.growth() == 0.0 and b.eta() is None


def test_bloat_ignores_a_session_that_is_not_filling():
    b = park.Bloat()
    for pct in (30.0, 30.0, 30.0, 29.0):
        b.add(1000, pct)
    assert b.eta() is None and b.reason() is None


# ── the app flow ────────────────────────────────────────────────────────────

pytest.importorskip("textual")
pytest.importorskip("claude_agent_sdk")
from orchestra.backend import ReplayBackend  # noqa: E402
from orchestra.entropy import Entropy  # noqa: E402
from ui.app import RabbleApp  # noqa: E402
from ui.prefs import Settings  # noqa: E402


def session(sid="s1"):
    return [ev.SessionStarted(sid, "claude-opus-5-5", "/x"), ev.TurnDone("end_turn"), ev.TurnDone("end_turn")]


class FakeLead(ReplayBackend):
    """Replays turns, and writes the handoff when it gets the park prompt, as the lead would
    during that turn. handoff_bytes=None: the lead never writes it."""

    handoff_bytes: int | None = 400
    carry = None

    async def send(self, text):
        if "PARK THIS SESSION" in text and self.handoff_bytes:
            path = Path(re.search(r"exactly this path: (\S+)", text).group(1))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("h" * self.handoff_bytes)
            self.path = path
        await super().send(text)


def make(tmp_path, handoff=400, **prefs):
    made, settings = [], Settings(**prefs)

    def factory(resume, cont):
        b = FakeLead(session(f"s{len(made) + 1}"))
        b.handoff_bytes, b.carry = handoff, settings.carry
        made.append(b)
        return b

    return RabbleApp(factory, cwd=str(tmp_path), settings=settings), made


async def until(pilot, cond, tries=150):
    for _ in range(tries):
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not met")


def sent(b):
    return [c[1] for c in b.calls if c[0] == "send"]


async def first_turn(app, pilot, made):
    await until(pilot, lambda: made and app.session_id)
    await pilot.press(*"hi", "enter")
    await until(pilot, lambda: not app._turn_open)


@pytest.mark.asyncio
async def test_parks_at_80_and_starts_fresh_from_the_handoff(tmp_path):
    app, made = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        made[0].emit(ev.Usage(1, None, 81.0))
        await until(pilot, lambda: any("PARK THIS SESSION" in t for t in sent(made[0])))
        path = made[0].path
        await until(pilot, lambda: len(made) == 2)                       # the fresh session
        await until(pilot, lambda: any("Read " in t for t in sent(made[1])))
        first = sent(made[1])[0]
        assert str(path) in first and "wait for me" in first
        assert isinstance(made[1].carry, Entropy)                       # score restarts at the baseline
        assert app.header.context_pct is None


@pytest.mark.asyncio
async def test_continue_pref_makes_the_fresh_session_carry_on(tmp_path):
    app, made = make(tmp_path, park_continue=True)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        made[0].emit(ev.Usage(1, None, 90.0))
        await until(pilot, lambda: any("PARK THIS SESSION" in t for t in sent(made[0])))
        await until(pilot, lambda: len(made) == 2 and sent(made[1]))
        assert "continue with its 'next' list" in sent(made[1])[0]


@pytest.mark.asyncio
async def test_missing_handoff_keeps_the_session(tmp_path):
    app, made = make(tmp_path, handoff=None)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        made[0].emit(ev.Usage(1, None, 85.0))
        await until(pilot, lambda: any("PARK THIS SESSION" in t for t in sent(made[0])))
        await until(pilot, lambda: app._park_blocked)                    # its turn ended with no file
        await pilot.pause(0.3)
        assert len(made) == 1
        assert app.backend is made[0]


@pytest.mark.asyncio
async def test_tiny_handoff_is_not_enough(tmp_path):
    app, made = make(tmp_path, handoff=20)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        made[0].emit(ev.Usage(1, None, 85.0))
        await until(pilot, lambda: any("PARK THIS SESSION" in t for t in sent(made[0])))
        await until(pilot, lambda: app._park_blocked)
        assert len(made) == 1


@pytest.mark.asyncio
async def test_ask_mode_only_tells_you(tmp_path):
    app, made = make(tmp_path, park="ask")
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        made[0].emit(ev.Usage(1, None, 88.0))
        await pilot.pause(0.3)
        assert not any("PARK THIS SESSION" in t for t in sent(made[0]))
        assert app._park_asked


@pytest.mark.asyncio
async def test_off_never_parks_and_below_80_does_not(tmp_path):
    for prefs, pct in (({"park": "off"}, 95.0), ({}, 79.0)):
        app, made = make(tmp_path, **prefs)
        async with app.run_test(size=(100, 40)) as pilot:
            await first_turn(app, pilot, made)
            made[0].emit(ev.Usage(1, None, pct))
            await pilot.pause(0.3)
            assert not any("PARK THIS SESSION" in t for t in sent(made[0]))


@pytest.mark.asyncio
async def test_usage_mid_turn_waits_for_the_turn_to_end(tmp_path):
    app, made = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        app._turn_open = True                                            # a turn is running
        made[0].emit(ev.Usage(1, None, 83.0))
        await until(pilot, lambda: app._park_pending)
        assert not any("PARK THIS SESSION" in t for t in sent(made[0]))
        made[0].emit(ev.TurnDone("end_turn"))                            # it ends: now park
        await until(pilot, lambda: any("PARK THIS SESSION" in t for t in sent(made[0])))


@pytest.mark.asyncio
async def test_parks_early_when_one_more_turn_would_overflow(tmp_path):
    app, made = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        for pct in (45.0, 60.0, 74.0):                                    # 14-15% a turn, still under 80
            made[0].emit(ev.Usage(1, None, pct))
            await pilot.pause(0.05)
        await until(pilot, lambda: any("PARK THIS SESSION" in t for t in sent(made[0])))
        await until(pilot, lambda: len(made) == 2)


@pytest.mark.asyncio
async def test_header_shows_capability_tier_and_park_eta(tmp_path):
    app, made = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        made[0].emit(ev.EntropyChanged(0.15, "STABLE", "trusted", "", 85, {"general": 85, "coding": 88}, False))
        for pct in (20.0, 30.0, 40.0):
            made[0].emit(ev.Usage(1, None, pct))
        await until(pilot, lambda: app.header.park_eta is not None)
        plain = app.header.plain
        assert "trusted 85" in plain and "code ▰▰▰▰▰▱ 88" in plain and "park in ~4" in plain


@pytest.mark.asyncio
async def test_warns_once_before_the_park(tmp_path):
    app, made = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        for pct in (71.0, 74.0):
            made[0].emit(ev.Usage(1, None, pct))
        await until(pilot, lambda: app._park_warned)
        assert not any("PARK THIS SESSION" in t for t in sent(made[0]))


@pytest.mark.asyncio
async def test_park_command_sets_modes_and_parks_now(tmp_path):
    app, made = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await first_turn(app, pilot, made)
        await app._park_command(["ask"])
        assert app.settings.park == "ask"
        await app._park_command(["continue", "on"])
        assert app.settings.park_continue is True
        await app._park_command(["continue", "off"])
        await app._park_command(["continue"])                            # park now, carrying on once
        await until(pilot, lambda: any("PARK THIS SESSION" in t for t in sent(made[0])))
        await until(pilot, lambda: len(made) == 2 and sent(made[1]))
        assert "continue with its 'next' list" in sent(made[1])[0]
        assert app.settings.park_continue is False                       # the "once" did not stick
