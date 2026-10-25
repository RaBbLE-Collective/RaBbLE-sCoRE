"""The engine (orchestra/): SDK message mapping, permission asks, fixture replay.
No live model calls: SDK messages are built by hand, fixtures were recorded with
`python -m orchestra.probe`. Needs the RaBbLE-UI venv (skips without the SDK):
  ~/.local/share/RaBbLE/rabble-ui/bin/python -m pytest tests"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sdk = pytest.importorskip("claude_agent_sdk")

from orchestra import events as ev  # noqa: E402
from orchestra.backend import ReplayBackend  # noqa: E402
from orchestra.permissions import Permissions, session_rules  # noqa: E402
from orchestra.sdk_map import SdkMapper, summarize  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "orchestra"


def init(session="s1"):
    return sdk.SystemMessage("init", {"session_id": session, "model": "haiku", "cwd": "/w"})


def assistant(*blocks, parent=None):
    return sdk.AssistantMessage(list(blocks), "haiku", parent_tool_use_id=parent)


def tool_result(tool_id, content, parent=None, error=False):
    return sdk.UserMessage([sdk.ToolResultBlock(tool_id, content, error)], parent_tool_use_id=parent)


def result(origin=None, error=False):
    return sdk.ResultMessage("success", 1, 1, error, 1, "s1", stop_reason="end_turn",
                             total_cost_usd=0.01, usage={"input_tokens": 5, "output_tokens": 7},
                             errors=["boom"] if error else None, origin=origin)


def run(mapper, *msgs):
    return [e for m in msgs for e in mapper.map(m)]


# ── mapping ─────────────────────────────────────────────────────────────────

def test_only_the_first_init_opens_the_session():
    m = SdkMapper()
    assert run(m, init(), init()) == [ev.SessionStarted("s1", "haiku", "/w")]
    assert run(SdkMapper(resumed=True), init()) == [ev.SessionResumed("s1")]


def test_lead_stream_deltas_thinking_then_block():
    m = SdkMapper()
    out = run(m,
              sdk.StreamEvent("u", "s1", {"type": "content_block_start", "content_block": {"type": "thinking"}}),
              sdk.StreamEvent("u", "s1", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "he"}}),
              sdk.StreamEvent("u", "s1", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "y"}}),
              assistant(sdk.ThinkingBlock("...", "sig"), sdk.TextBlock("hey")))
    assert out == [ev.Thinking(ev.LEAD), ev.TextDelta(ev.LEAD, "he"), ev.TextDelta(ev.LEAD, "y"),
                   ev.TextBlock(ev.LEAD, "hey")]


def test_organ_lifecycle_routes_by_parent_tool_use_id():
    m = SdkMapper()
    out = run(m,
              assistant(sdk.ToolUseBlock("A1", "Agent", {"subagent_type": "gobble", "description": "map it"})),
              sdk.TaskStartedMessage("task_started", {}, "T1", "map it", "u", "s1", tool_use_id="A1"),
              sdk.StreamEvent("u", "s1", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
                              parent_tool_use_id="A1"),
              assistant(sdk.ToolUseBlock("B1", "Grep", {"pattern": "x"}), parent="A1"),
              tool_result("B1", "3 hits", parent="A1"),
              sdk.TaskProgressMessage("task_progress", {}, "T1", "map it", {"total_tokens": 9, "tool_uses": 1,
                                      "duration_ms": 5}, "u", "s1", last_tool_name="Grep"),
              assistant(sdk.TextBlock("it is in beam.py"), parent="A1"),
              sdk.TaskNotificationMessage("task_notification", {}, "T1", "completed", "/o", "", "u", "s1"),
              tool_result("A1", [{"type": "text", "text": "hand-back"}]))
    assert out == [
        ev.ToolCall(ev.LEAD, "A1", "Agent", {"subagent_type": "gobble", "description": "map it"}),
        ev.OrganStarted("gobble", "A1", None, "map it"),
        ev.ToolCall("gobble", "B1", "Grep", {"pattern": "x"}),
        ev.ToolResult("gobble", "B1", True, "3 hits"),
        ev.OrganProgress("gobble", "T1", {"total_tokens": 9, "tool_uses": 1, "duration_ms": 5}, "Grep"),
        ev.TextBlock("gobble", "it is in beam.py"),
        ev.OrganDone("gobble", "T1", "completed", "it is in beam.py"),
        ev.ToolResult(ev.LEAD, "A1", True, "hand-back"),  # no second OrganDone
    ]


def test_killed_organ_via_task_updated_and_other_tasks_ignored():
    m = SdkMapper()
    run(m, assistant(sdk.ToolUseBlock("A1", "Agent", {"subagent_type": "nibble"})),
        sdk.TaskStartedMessage("task_started", {}, "T1", "", "u", "s1", tool_use_id="A1"))
    assert run(m, sdk.TaskUpdatedMessage("task_updated", {}, "T1", {}, status="running")) == []
    assert run(m, sdk.TaskUpdatedMessage("task_updated", {}, "T1", {}, status="killed")) == \
        [ev.OrganDone("nibble", "T1", "killed", "")]
    # a background Bash task is not an organ
    assert run(m, sdk.TaskNotificationMessage("task_notification", {}, "T9", "completed", "/o", "ok", "u", "s1")) == []


def test_failed_foreground_organ_ends_on_its_tool_result():
    m = SdkMapper()
    run(m, assistant(sdk.ToolUseBlock("A1", "Task", {"subagent_type": "cobble"})))
    out = run(m, tool_result("A1", "crashed", error=True))
    assert out == [ev.ToolResult(ev.LEAD, "A1", False, "crashed"), ev.OrganDone("cobble", "", "failed", "crashed")]


def test_result_turn_origin_usage_and_errors():
    m = SdkMapper()
    assert run(m, result()) == [ev.Usage(12, 0.01), ev.TurnDone("end_turn", "observer")]
    assert run(m, result({"kind": "task-notification"}))[-1] == ev.TurnDone("end_turn", "background")
    assert run(m, result(error=True))[0] == ev.Error("boom")


def test_rate_limits_and_summaries():
    m = SdkMapper()
    info = sdk.RateLimitInfo("allowed_warning", resets_at=100)
    assert run(m, sdk.RateLimitEvent(info, "u", "s1")) == [ev.RateLimited("warning", 100.0)]
    assert summarize("a\n  b") == "a b"
    assert len(summarize("x" * 500)) == 200


# ── permissions ─────────────────────────────────────────────────────────────

def ctx(**kw):
    base = dict(tool_use_id="t1", agent_id=None, suggestions=[], title="Run?", display_name=None,
                description=None, blocked_path=None)
    return SimpleNamespace(**{**base, **kw})


def test_permission_ask_round_trip():
    async def go():
        seen = []
        perms = Permissions(seen.append, lambda a: "gobble" if a else ev.LEAD)
        task = asyncio.create_task(perms.can_use_tool("Bash", {"command": "ls"}, ctx(agent_id="ag")))
        await asyncio.sleep(0)
        assert seen[0].organ == "gobble" and seen[0].id == "t1" and perms.pending == ["t1"]
        assert perms.answer("t1", ev.PermissionReply("deny", "nope"))
        res = await task
        assert isinstance(res, sdk.PermissionResultDeny) and res.message == "nope"
        assert perms.pending == [] and not perms.answer("t1", ev.PermissionReply("allow"))
    asyncio.run(go())


def test_deny_all_releases_every_ask():
    async def go():
        perms = Permissions(lambda e: None, lambda a: ev.LEAD)
        tasks = [asyncio.create_task(perms.can_use_tool("Edit", {}, ctx(tool_use_id=i))) for i in "ab"]
        await asyncio.sleep(0)
        perms.deny_all()
        assert all(isinstance(r, sdk.PermissionResultDeny) for r in await asyncio.gather(*tasks))
    asyncio.run(go())


def test_allow_session_never_writes_to_disk():
    disk = sdk.PermissionUpdate(type="addRules", rules=[sdk.types.PermissionRuleValue("Bash", "npm test:*")],
                                behavior="allow", destination="projectSettings")
    mode = sdk.PermissionUpdate(type="setMode", mode="acceptEdits", destination="localSettings")
    rules = session_rules("Bash", {"command": "npm test"}, [disk, mode])
    assert [r.destination for r in rules] == ["session"]
    assert rules[0].rules[0].rule_content == "npm test:*"
    # no suggestion: the exact command, never all of Bash
    fallback = session_rules("Bash", {"command": "rm -rf build"}, [])
    assert fallback[0].rules[0].rule_content == "rm -rf build" and fallback[0].destination == "session"


# ── replay ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["tool-turn", "organ-turn"])
def test_recorded_fixtures_replay_turn_by_turn(name):
    async def go():
        backend = ReplayBackend.from_file(FIXTURES / f"{name}.jsonl")
        await backend.start()
        await backend.send("go")
        await backend.close()
        return [e async for e in backend.events()]
    out = asyncio.run(go())
    assert isinstance(out[0], ev.SessionStarted)
    assert any(isinstance(e, ev.TurnDone) and e.origin == "observer" for e in out)
    if name == "organ-turn":
        organs = {e.organ for e in out if isinstance(e, (ev.OrganStarted, ev.OrganDone))}
        assert len(organs) == 1 and ev.LEAD not in organs
