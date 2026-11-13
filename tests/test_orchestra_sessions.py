"""orchestra/sessions.py: SDK session infos become SessionRows, failures become [].
Run: ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests"""

import os
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestra import sessions  # noqa: E402


def info(sid, ms, **kw):
    return NS(session_id=sid, last_modified=ms, summary=kw.pop("summary", ""), cwd="/p", **kw)


def test_rows_sorted_newest_first(monkeypatch):
    monkeypatch.setattr(sessions, "_list", lambda cwd, limit: [
        info("old", 1000, summary="old one"), info("new", 5000, custom_title="new one")])
    rows = sessions.recent_sessions("/p")
    assert [r.id for r in rows] == ["new", "old"]
    assert rows[0].title == "new one" and rows[0].updated == 5.0 and rows[0].turns is None


def test_title_trimmed_and_fallback(monkeypatch):
    monkeypatch.setattr(sessions, "_list", lambda cwd, limit: [
        info("a", 1, summary="x\n" * 100), info("b", 2, first_prompt="hi there")])
    rows = {r.id: r for r in sessions.recent_sessions("/p")}
    assert len(rows["a"].title) <= sessions.TITLE_MAX and "\n" not in rows["a"].title
    assert rows["b"].title == "hi there"


def test_limit(monkeypatch):
    monkeypatch.setattr(sessions, "_list", lambda c, l: [info(str(i), i) for i in range(10)])
    assert len(sessions.recent_sessions("/p", limit=3)) == 3


def test_errors_and_empty(monkeypatch):
    def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(sessions, "_list", boom)
    assert sessions.recent_sessions("/p") == []
    monkeypatch.setattr(sessions, "_list", lambda c, l: None)
    assert sessions.recent_sessions("/p") == []


@pytest.mark.skipif(os.environ.get("RUN_SESSION_SMOKE") != "1", reason="real ~/.claude read")
def test_smoke_real():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    assert isinstance(sessions.recent_sessions(root), list)
