"""Settings persistence + the new commands (/model picker, /crew, /trust, /help)."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("textual")
pytest.importorskip("claude_agent_sdk")

from orchestra.backend import ReplayBackend  # noqa: E402
from ui.app import RabbleApp  # noqa: E402
from ui.prefs import Settings, resolve_model  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "orchestra"


def test_settings_roundtrip_and_bad_file(tmp_path):
    path = tmp_path / "ui.json"
    s = Settings.load(path)
    assert (s.model, s.crew, s.trust, s.mode) == (None, False, None, "default")  # crew off by default
    s.model, s.crew, s.trust = "opus", True, "trusted"
    s.save()
    again = Settings.load(path)
    assert (again.model, again.crew, again.trust) == ("opus", True, "trusted")
    path.write_text("{not json", encoding="utf-8")
    assert Settings.load(path).crew is False
    path.write_text('{"crew": "yes", "trust": "root", "mode": "bypass"}', encoding="utf-8")
    s = Settings.load(path)
    assert (s.crew, s.trust, s.mode) == (False, None, "default")


def test_resolve_model():
    assert resolve_model("default") is None
    assert resolve_model("Opus") == "opus"
    assert resolve_model("claude-opus-5-5") == "claude-opus-5-5"


async def until(pilot, cond, tries=100):
    for _ in range(tries):
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not met")


def make(tmp_path):
    made = []

    def factory(resume, cont):
        made.append((resume, cont))
        b = ReplayBackend.from_file(FIX / "tool-turn.jsonl")
        made.append(b)
        return b

    settings = Settings(path=tmp_path / "ui.json")
    return RabbleApp(factory, cwd=str(tmp_path), settings=settings), made, settings


@pytest.mark.asyncio
async def test_model_crew_trust_commands(tmp_path):
    app, made, settings = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await until(pilot, lambda: app.session_id)
        await pilot.press(*"/model haiku", "enter")
        await until(pilot, lambda: settings.model == "haiku")
        assert "trust strict" in app.header.plain and "crew off" in app.header.plain
        await pilot.press(*"/trust trusted", "enter")
        await until(pilot, lambda: settings.trust == "trusted")
        assert "trust trusted" in app.header.plain
        n = len(made)
        await pilot.press(*"/crew on", "enter")
        await until(pilot, lambda: settings.crew and len(made) > n)
        assert made[n][0] == app.session_id  # restarted on the same session, not a fresh one
        assert "crew on" in app.header.plain
    saved = Settings.load(tmp_path / "ui.json")
    assert (saved.model, saved.crew, saved.trust) == ("haiku", True, "trusted")


@pytest.mark.asyncio
async def test_slash_completion_and_help(tmp_path):
    app, made, settings = make(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press(*"/cr")
        await until(pilot, lambda: app.prompt.dropdown.display)
        await pilot.press("tab")
        assert app.composer.text == "/crew "
        await pilot.press(*"off", "enter")
        await pilot.pause(0.1)
        await pilot.press(*"/help", "enter")
        await until(pilot, lambda: "/trust" in " ".join(str(w.render()) for w in app.transcript.children))
