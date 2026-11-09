"""Engine config (orchestra/config.py): file merge, validation, SDK hand-off.
  ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests/test_orchestra_config.py"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestra.config import EngineConfig  # noqa: E402


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_defaults_pass_nothing(tmp_path):
    cfg = EngineConfig.load(tmp_path, tmp_path / "none.json")
    assert cfg.sdk_kwargs() == {} and cfg.problems == [] and cfg.summary() == "engine defaults"


def test_project_file_merges_over_user_file(tmp_path):
    user = write(tmp_path / "u.json", {"mcp_servers": {"a": {"command": "x"}}, "disallowed_tools": ["WebFetch"],
                                       "effort": "low", "max_turns": 10})
    write(tmp_path / "proj" / ".rabble" / "engine.json", {"mcp_servers": {"b": {"url": "https://h"}},
          "disallowed_tools": ["WebFetch", "Task"], "effort": "high", "thinking": 8000, "plugins": ["~/p"]})
    kw = EngineConfig.load(tmp_path / "proj", user).sdk_kwargs()
    assert set(kw["mcp_servers"]) == {"a", "b"}
    assert kw["disallowed_tools"] == ["WebFetch", "Task"]
    assert kw["effort"] == "high" and kw["max_turns"] == 10
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert kw["plugins"] == [{"type": "local", "path": os.path.expanduser("~/p")}]


def test_bad_entries_are_named_and_dropped(tmp_path):
    user = write(tmp_path / "u.json", {"mcp_servers": {"ok": {"command": "x"}, "bad": {}}, "effort": "ludicrous",
                                       "max_budget_usd": -1, "max_turns": True, "nope": 1, "thinking": "adaptive"})
    cfg = EngineConfig.load(None, user)
    assert list(cfg.mcp_servers) == ["ok"] and cfg.effort is None and cfg.max_budget_usd is None and cfg.max_turns is None
    assert cfg.sdk_kwargs()["thinking"] == {"type": "adaptive"}
    assert len(cfg.problems) == 5
    assert "5 config problem(s)" in cfg.summary()


def test_unreadable_file_never_raises(tmp_path):
    cfg = EngineConfig.load(None, write(tmp_path / "u.json", "{not json"))
    assert cfg.problems and cfg.sdk_kwargs() == {}


def test_backend_options_carry_the_config(tmp_path):
    import pytest
    pytest.importorskip("claude_agent_sdk")
    from orchestra.backend import ClaudeCodeBackend
    cfg = EngineConfig.load(None, write(tmp_path / "u.json", {"mcp_servers": {"a": {"command": "x"}}, "max_budget_usd": 2,
                                                                 "fallback_model": "sonnet", "add_dirs": ["/tmp"]}))
    o = ClaudeCodeBackend(".", engine=cfg).options()
    assert o.mcp_servers["a"] == {"command": "x"} and set(o.mcp_servers) == {"a", "rabble"} and o.max_budget_usd == 2.0
    assert o.fallback_model == "sonnet" and o.add_dirs == ["/tmp"]
    assert set(ClaudeCodeBackend(".").options().mcp_servers) == {"rabble"}   # only the engine's own end_session


def test_thinking_display_alone_implies_adaptive_and_never_rides_on_disabled(tmp_path):
    cfg = EngineConfig.load(None, write(tmp_path / "a.json", {"thinking_display": "summarized"}))
    assert cfg.sdk_kwargs()["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "summarized" in cfg.summary()
    cfg = EngineConfig.load(None, write(tmp_path / "b.json", {"thinking": 4000, "thinking_display": "summarized"}))
    assert cfg.sdk_kwargs()["thinking"] == {"type": "enabled", "budget_tokens": 4000, "display": "summarized"}
    cfg = EngineConfig.load(None, write(tmp_path / "c.json", {"thinking": "disabled", "thinking_display": "summarized"}))
    assert cfg.sdk_kwargs()["thinking"] == {"type": "disabled"}
    assert EngineConfig.load(None, write(tmp_path / "d.json", {"thinking_display": "loud"})).problems
