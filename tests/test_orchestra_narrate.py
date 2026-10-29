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
    assert narrate.tool("Bash", {"command": "cd x && pytest -q tests"}) == "Running the tests"
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
