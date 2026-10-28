"""The engine/face event contract (orchestra/events.py): every event round-trips
through JSON, which is what fixture replay and `python -m orchestra.probe` rely on.
Run: server/.venv/bin/python -m pytest tests"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestra import events as ev  # noqa: E402

SAMPLES = [
    ev.SessionStarted("s1", "opus", "/home/x"),
    ev.SessionResumed("s1"),
    ev.TextDelta(ev.LEAD, "hel"),
    ev.TextBlock("gobble", "found it"),
    ev.Thinking(ev.LEAD),
    ev.ToolCall(ev.LEAD, "tu1", "Bash", {"command": "ls"}),
    ev.ToolResult(ev.LEAD, "tu1", True, "3 files"),
    ev.PermissionAsk("p1", ev.LEAD, "Bash", {"command": "rm x"}, title="Run rm?",
                     suggestions=[{"type": "addRules", "destination": "session"}]),
    ev.OrganStarted("gobble", "tu2", "t1", "map the beam reconnect"),
    ev.OrganProgress("gobble", "t1", {"tokens": 10}, "Grep"),
    ev.OrganDone("gobble", "t1", "completed", "it lives in beam.py"),
    ev.Usage(1200, 0.01, 4.5),
    ev.RateLimited("warning", 1761400000.0),
    ev.EntropyChanged(0.55, "ELEVATED", "strict", "context compacted"),
    ev.ModelUsed("claude-opus-5-5"),
    ev.TurnDone("end_turn", "background"),
    ev.Error("boom"),
]


def test_every_event_kind_has_a_sample():
    assert {type(e).kind for e in SAMPLES} == set(ev._KINDS)


@pytest.mark.parametrize("event", SAMPLES, ids=lambda e: e.kind)
def test_round_trip(event):
    line = event.to_json()
    assert "\n" not in line
    assert ev.from_json(line) == event


def test_type_tag_and_defaults():
    d = ev.TurnDone("end_turn").to_dict()
    assert d == {"type": "TurnDone", "stop_reason": "end_turn", "origin": "observer"}
    assert ev.PermissionAsk("p", ev.LEAD, "Edit", {}).suggestions == []


def test_unknown_keys_dropped_unknown_type_refused():
    assert ev.from_dict({"type": "Error", "text": "x", "gone": 1}) == ev.Error("x")
    with pytest.raises(ValueError):
        ev.from_dict({"type": "Nope"})


def test_events_are_frozen():
    with pytest.raises(AttributeError):
        ev.Error("x").text = "y"
