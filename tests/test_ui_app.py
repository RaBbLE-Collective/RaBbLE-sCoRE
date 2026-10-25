"""RabbleApp driven by ReplayBackend over recorded fixtures: no live model calls.
  ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests/test_ui_app.py"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("textual")
pytest.importorskip("claude_agent_sdk")

from textual.widgets import Markdown  # noqa: E402

import ui.app as appmod  # noqa: E402
from orchestra.backend import ReplayBackend  # noqa: E402
from ui.app import RabbleApp  # noqa: E402
from ui.widgets.tool_card import ToolCard  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "orchestra"


def make(fixture, tmp_path):
    holder = {}

    def factory(resume, cont):
        holder["b"] = ReplayBackend.from_file(FIX / fixture)
        return holder["b"]

    return RabbleApp(factory, cwd=str(tmp_path)), holder


async def until(pilot, cond, tries=100):
    for _ in range(tries):
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not met")


async def say(pilot, text):
    await pilot.press(*text, "enter")


def md_sources(app):
    return [m.source for m in app.transcript.query(Markdown)]


def calls(holder):
    return [c[0] for c in holder["b"].calls]


@pytest.mark.asyncio
async def test_text_rendered_once_and_header(tmp_path):
    app, h = make("tool-turn.jsonl", tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await until(pilot, lambda: "haiku" in app.header.plain)
        await say(pilot, "hi")
        await until(pilot, lambda: app.header.cost is not None and md_sources(app))
        await pilot.pause(0.1)
        assert md_sources(app) == ["8"]
        await say(pilot, "again")
        await until(pilot, lambda: app.header.context_pct == 4.0)
        plain = app.header.plain
        assert "claude-haiku-5-5" in plain
        assert "$0.0059 API equiv" in plain
        assert "ctx 4%" in plain


@pytest.mark.asyncio
async def test_tool_card_with_result(tmp_path):
    app, h = make("tool-turn.jsonl", tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await say(pilot, "hi")
        await until(pilot, lambda: list(app.transcript.query(ToolCard)))
        await until(pilot, lambda: app.transcript.query(ToolCard).first().result)
        card = app.transcript.query(ToolCard).first()
        assert "Bash" in card.title and "ls RaBbLE-sCoRE" in card.title and "→ 8" in card.title
        assert card.has_class("-ok")


@pytest.mark.asyncio
async def test_organ_turn_notices(tmp_path):
    app, h = make("organ-turn.jsonl", tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await say(pilot, "who defines SdkMapper")
        await until(pilot, lambda: "sdk_map.py" in md_sources(app))
        texts = " ".join(str(w.render()) for w in app.transcript.query("Static.notice"))
        assert "Explore started" in texts and "Explore completed" in texts
        assert md_sources(app).count("sdk_map.py") == 2  # organ block + lead answer
        out = Path("/home/rabble/RaBbLE-Collective/RaBbLE-BaBbLE/tmp/face")
        if out.is_dir():
            app.save_screenshot("ui-organ-turn.svg", path=str(out))


@pytest.mark.asyncio
async def test_escape_model_mode(tmp_path):
    app, h = make("tool-turn.jsonl", tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("escape")
        await until(pilot, lambda: "interrupt" in calls(h))
        await say(pilot, "/model opus")
        await until(pilot, lambda: ("set_model", "opus") in h["b"].calls)
        assert "opus" in app.header.plain
        await pilot.press("shift+tab")
        await until(pilot, lambda: ("set_permission_mode", "acceptEdits") in h["b"].calls)
        assert "acceptEdits" in app.header.plain


@pytest.mark.asyncio
async def test_unknown_slash_passes_through(tmp_path):
    app, h = make("tool-turn.jsonl", tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await say(pilot, "/commit now")
        await until(pilot, lambda: ("send", "/commit now") in h["b"].calls)


@pytest.mark.asyncio
async def test_ctrl_c_twice_exits(tmp_path):
    app, h = make("tool-turn.jsonl", tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert app.is_running
        assert any("again to quit" in str(w.render()) for w in app.transcript.query("Static.notice"))
        await pilot.press("ctrl+c")
        await pilot.pause(0.2)
    assert "close" in calls(h)


@pytest.mark.asyncio
async def test_permission_fallback_denies(tmp_path, monkeypatch):
    from orchestra import events as ev
    real = appmod._optional
    monkeypatch.setattr(appmod, "_optional", lambda m, n: None if n == "PermissionScreen" else real(m, n))
    app, h = make("tool-turn.jsonl", tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await app._handle(ev.PermissionAsk(id="p1", organ="rabble", tool="Bash", input={}))
        answers = [c for c in h["b"].calls if c[0] == "answer"]
        assert answers and answers[0][1][0] == "p1" and answers[0][1][1].decision == "deny"


@pytest.mark.asyncio
async def test_at_completion(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "notes.cfg").write_text("x")
    app, h = make("tool-turn.jsonl", tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("@", "n", "o")
        await until(pilot, lambda: app.prompt.dropdown.display)
        assert app.prompt.dropdown.option_count == 1
        await pilot.press("tab")
        assert app.composer.text == "@notes.md "
