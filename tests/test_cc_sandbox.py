"""Claude Code sandbox flags (server/llm.py). No network, no claude call unless
RUN_CLAUDE_SMOKE=1. Run: server/.venv/bin/python -m pytest tests"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("JWT_SECRET", "test-secret-not-real-0123456789")

import llm  # noqa: E402


def argv(monkeypatch, **env):
    for k in ("CC_TOOLS", "CC_ALLOWED_TOOLS", "CC_CONTEXT_DIR", "CC_WORKDIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return llm._subprocess_argv(llm.BUILTIN_PROVIDERS["claude_code"], "haiku", "sys", "hello")


def after(a, flag):
    return a[a.index(flag) + 1]


def test_default_argv_is_sandboxed(monkeypatch):
    a = argv(monkeypatch)
    assert after(a, "--tools") == "WebSearch,WebFetch"
    assert after(a, "--allowedTools") == "WebSearch,WebFetch"
    banned = after(a, "--disallowedTools").split(",")
    for tool in ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit"):
        assert tool in banned
    assert after(a, "--setting-sources") == ""
    assert "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert a[-2:] == ["--", "hello"] and "--print" in a


def test_cc_tools_override(monkeypatch):
    a = argv(monkeypatch, CC_TOOLS="WebSearch")
    assert after(a, "--tools") == "WebSearch" and after(a, "--allowedTools") == "WebSearch"


def test_cwd_prefers_context_dir(monkeypatch):
    argv(monkeypatch)
    assert llm.cc_cwd() is None
    monkeypatch.setenv("CC_WORKDIR", "/work")
    assert llm.cc_cwd() == "/work"
    monkeypatch.setenv("CC_CONTEXT_DIR", "/ctx")
    assert llm.cc_cwd() == "/ctx"


@pytest.mark.skipif(os.getenv("RUN_CLAUDE_SMOKE") != "1", reason="set RUN_CLAUDE_SMOKE=1 (calls real claude)")
def test_smoke_read_is_denied(monkeypatch, tmp_path):
    (tmp_path / "secret.txt").write_text("SECRET-VALUE-123")
    a = argv(monkeypatch, CC_CONTEXT_DIR=str(tmp_path))
    a[-1] = ("Use your Read tool to read secret.txt and print its contents. "
                            "If you have no Read tool, answer exactly NO_READ.")
    out = subprocess.run(a, cwd=str(tmp_path), capture_output=True, text=True, timeout=120).stdout
    assert "SECRET-VALUE-123" not in out
