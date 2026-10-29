"""orchestra/narrate.py: one plain sentence per tool call, never leaking secrets or huge args."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestra import narrate  # noqa: E402


def test_file_tools_show_the_last_two_path_parts():
    assert narrate.tool("Read", {"file_path": "/home/rabble/RaBbLE-sCoRE/orchestra/events.py"}) == "Reading `orchestra/events.py`"
    assert narrate.tool("Edit", {"file_path": "a.py"}) == "Editing `a.py`"
    assert narrate.tool("Write", {}) == "Writing a file"


def test_bash_is_described_by_kind_not_echoed():
    assert narrate.tool("Bash", {"command": "pytest -q tests"}) == "Running the tests"
    assert narrate.tool("Bash", {"command": "cargo test --release"}) == "Running the tests"
    assert narrate.tool("Bash", {"command": "git status"}) == "Running a git command"
    assert narrate.tool("Bash", {"command": "frobnicate --now"}) == "Running a shell command"


def test_organ_handoff_names_the_organ_and_task():
    assert narrate.tool("Agent", {"subagent_type": "gobble", "description": "Map the beam reconnect"}) == \
        "Asking the gobble organ to map the beam reconnect"
    assert narrate.tool("Task", {}) == "Asking the agent organ for help"


def test_search_tools_and_misc():
    assert narrate.tool("Grep", {"pattern": "def tool"}) == "Searching the code for `def tool`"
    assert narrate.tool("Glob", {"pattern": "**/*.py"}) == "Looking for files matching `**/*.py`"
    assert narrate.tool("TodoWrite", {"todos": []}) == "Updating the task list"
    assert narrate.tool("mcp__notes__add_note", {"x": 1}) == "Using add note from notes"


def test_unknown_tool_falls_back():
    assert narrate.tool("Frobnicate", {"a": 1}) == "Using Frobnicate"
    assert narrate.tool("", None) == "Using a tool"
    assert narrate.tool("Read", "not a dict") == "Reading a file"


def test_secrets_and_huge_args_never_leak():
    key = "sk-abcdefghijklmnop1234"
    assert key not in narrate.tool("Grep", {"pattern": f"api_key={key}"})
    assert narrate.tool("Bash", {"command": f"curl -H 'Authorization: Bearer {key}' x"}) == "Running a shell command"
    out = narrate.tool("Grep", {"pattern": "x" * 5000})
    assert len(out) <= narrate.MAX_SENTENCE
    assert len(narrate.tool("Agent", {"subagent_type": "g", "description": "y" * 5000})) <= narrate.MAX_SENTENCE


def test_permission_wait():
    assert narrate.permission("Bash") == "Waiting for your answer on a permission"


def test_chained_commands_are_judged_by_their_first_word_only():
    assert narrate.tool("Bash", {"command": "echo pytest; rm -rf ~"}) == "Running a shell command"
    assert narrate.tool("Bash", {"command": "cd x && pytest -q"}) == "Running a shell command"
    assert narrate.tool("Bash", {"command": "git log | head"}) == "Running a git command"


def test_more_secret_shapes_are_masked_everywhere():
    secrets = ["gsk_abcdefghijklmnop1234", "glpat-abcdefghij1234567890", "hf_abcdefghijklmnop12", "AIzaSyA1234567890abcdefghij",
               "sk_live_abcdefghijkl1234", "sk_test_abcdefghijkl1234", "npm_abcdefghijklmnop1234",
               "0123456789abcdef0123456789abcdef01234567", "Zk3Jq9Xv2Lm8Np4Rt6Yw1Bc5Dg7Hs0Fa9E2"]
    for sec in secrets:
        for out in (narrate.tool("Read", {"file_path": f"/home/x/{sec}/a.py"}), narrate.tool("Grep", {"pattern": sec}),
                    narrate.tool("Glob", {"pattern": f"**/{sec}*"}), narrate.tool("WebSearch", {"query": f"find {sec}"}),
                    narrate.tool("Skill", {"skill": sec}), narrate.tool("Agent", {"subagent_type": "g", "description": f"use {sec}"})):
            assert sec not in out and sec[:12] not in out, (sec, out)


def test_ordinary_words_and_names_survive():
    assert narrate.tool("Read", {"file_path": "/x/token_refresh.py"}) == "Reading `x/token_refresh.py`"
    assert narrate.tool("Grep", {"pattern": "token"}) == "Searching the code for `token`"
    assert narrate.tool("Agent", {"subagent_type": "g", "description": "API docs review"}) == "Asking the g organ to API docs review"
    assert narrate.tool("Read", {"file_path": "/x/a-very-long-readable-name-with-only-letters-in-it.md"}).count("[hidden]") == 0


def test_control_and_bidi_characters_are_stripped():
    out = narrate.tool("Grep", {"pattern": "a\x1b[31m\u202eb\x85c"})
    assert not any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f or c == "\u202e" for c in out)


def test_tool_never_raises_and_mcp_edges():
    assert narrate.tool(None, {}) == "Using a tool"
    assert narrate.tool("mcp__a__", {}) == "Using a"
    assert narrate.tool("Read", {"file_path": object()}).startswith("Reading")
