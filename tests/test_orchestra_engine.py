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
    """Mapped events, minus ModelUsed (entropy bookkeeping, tested in test_orchestra_entropy)."""
    return [e for m in msgs for e in mapper.map(m) if not isinstance(e, ev.ModelUsed)]


# ── mapping ─────────────────────────────────────────────────────────────────

def test_only_the_first_init_opens_the_session():
    m = SdkMapper()
    assert run(m, init(), init()) == [ev.Capabilities([], [], [], [], []), ev.SessionStarted("s1", "haiku", "/w")]
    assert run(SdkMapper(resumed=True), init())[1:] == [ev.SessionResumed("s1")]


def test_init_reports_commands_skills_and_load_failures():
    msg = sdk.SystemMessage("init", {"session_id": "s1", "model": "haiku", "cwd": "/w",
                                     "slash_commands": ["compact", "endsession"], "skills": ["endsession"],
                                     "mcp_servers": [{"name": "a", "status": "connected"}, {"name": "b", "status": "failed"}],
                                     "plugins": [{"name": "mine", "path": "/p"}],
                                     "plugin_errors": [{"plugin": "inline[0]", "type": "path-not-found", "message": "no such dir"}]})
    caps = run(SdkMapper(), msg)[0]
    assert caps.slash_commands == ["compact", "endsession"] and caps.plugins == ["mine"]
    assert caps.problems == ["mcp b: failed", "plugin inline[0]: no such dir"]


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
        ev.Narration(ev.LEAD, "Asking the gobble organ to map it", "A1"),
        ev.OrganStarted("gobble", "A1", None, "map it", False),
        ev.ToolCall("gobble", "B1", "Grep", {"pattern": "x"}),
        ev.Narration("gobble", "Searching the code for `x`", "B1"),
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


def test_a_question_answer_rides_back_in_the_tool_input():
    async def go():
        perms = Permissions(lambda e: None, lambda a: ev.LEAD)
        qs = {"questions": [{"question": "Which db?", "options": []}]}
        task = asyncio.create_task(perms.can_use_tool("AskUserQuestion", qs, ctx()))
        await asyncio.sleep(0)
        perms.answer("t1", ev.PermissionReply("allow", "", {"Which db?": "sqlite"}))
        res = await task
        assert isinstance(res, sdk.PermissionResultAllow)
        assert res.updated_input == {**qs, "answers": {"Which db?": "sqlite"}}
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


def test_options_agents_are_sdk_dataclasses():
    """The SDK runs asdict() on every agent; roster's CLI-style dicts crashed the pump
    ("asdict() should be called on dataclass instances", then "Not connected")."""
    from dataclasses import asdict

    from orchestra.backend import ClaudeCodeBackend
    agents = {"scribe": {"description": "d", "prompt": "p", "model": "haiku", "tools": ["Read"]}}
    opts = ClaudeCodeBackend(".", agents=agents).options()
    assert {k: {a: b for a, b in asdict(v).items() if b is not None} for k, v in opts.agents.items()} == agents
    assert ClaudeCodeBackend(".").options().agents is None


def _tdelta(t):
    return sdk.StreamEvent("u", "s1", {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": t}})


def test_thinking_text_is_batched_for_the_lead_and_whole_for_organs():
    m = SdkMapper()
    out = run(m,
              sdk.StreamEvent("u", "s1", {"type": "content_block_start", "content_block": {"type": "thinking"}}),
              _tdelta("Check "), _tdelta("the seam."),
              sdk.StreamEvent("u", "s1", {"type": "content_block_stop"}),
              assistant(sdk.ThinkingBlock("Check the seam.", "sig"), sdk.TextBlock("ok")),
              assistant(sdk.ToolUseBlock("A1", "Agent", {"subagent_type": "gobble", "description": "map it"})),
              assistant(sdk.ThinkingBlock("Grep first.", "sig"), parent="A1"),
              assistant(sdk.ThinkingBlock("", "sig"), parent="A1"))
    assert [e for e in out if isinstance(e, ev.ThinkingText)] == [
        ev.ThinkingText(ev.LEAD, "Check the seam."), ev.ThinkingText("gobble", "Grep first.")]
    assert out[0] == ev.Thinking(ev.LEAD)   # the pulse is still sent


def test_thinking_events_per_block_stay_bounded():
    m = SdkMapper()
    out = run(m, sdk.StreamEvent("u", "s1", {"type": "content_block_start", "content_block": {"type": "thinking"}}),
              *[_tdelta("word ") for _ in range(2000)],
              sdk.StreamEvent("u", "s1", {"type": "content_block_stop"}))
    texts = [e for e in out if isinstance(e, ev.ThinkingText)]
    assert len(texts) <= 2000 * 5 // 200 + 2 and "".join(e.text for e in texts) == "word " * 2000


def test_pending_thinking_flushes_before_a_non_thinking_event():
    m = SdkMapper()
    run(m, _tdelta("half a thought"))
    out = run(m, sdk.StreamEvent("u", "s1", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}))
    assert out == [ev.ThinkingText(ev.LEAD, "half a thought"), ev.TextDelta(ev.LEAD, "hi")]


def test_every_tool_call_gets_a_narration_with_its_id():
    out = run(SdkMapper(), assistant(sdk.ToolUseBlock("R1", "Read", {"file_path": "/home/x/orchestra/events.py"})))
    assert out[-1] == ev.Narration(ev.LEAD, "Reading `orchestra/events.py`", "R1")


def narration_turn_events():
    """The thinking + tool + answer turn, fed through SdkMapper (the source of narration-turn.jsonl)."""
    m = SdkMapper()
    thought = "The user wants the events file. Read it first."
    msgs = [sdk.SystemMessage("init", {"session_id": "s1", "model": "haiku", "cwd": "/w"}),
            sdk.StreamEvent("u", "s1", {"type": "content_block_start", "content_block": {"type": "thinking"}}),
            _tdelta("The user wants the events file. "), _tdelta("Read it first."),
            sdk.StreamEvent("u", "s1", {"type": "content_block_stop"}),
            assistant(sdk.ThinkingBlock(thought, "sig"), sdk.ToolUseBlock("tu1", "Read", {"file_path": "/w/orchestra/events.py"})),
            tool_result("tu1", "242 lines"),
            sdk.StreamEvent("u", "s1", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "It defines "}}),
            sdk.StreamEvent("u", "s1", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "the event contract."}}),
            assistant(sdk.TextBlock("It defines the event contract.")),
            sdk.ResultMessage("success", 10, 5, False, 1, "s1", stop_reason="end_turn",
                              usage={"input_tokens": 5, "output_tokens": 5}, total_cost_usd=0.001)]
    return [e for msg in msgs for e in m.map(msg)]


def test_narration_fixture_is_current_mapper_output():
    """tests/fixtures/orchestra/narration-turn.jsonl is exactly narration_turn_events(); regenerate with
      python -c "import sys; sys.path[:0]=['.','tests']; from test_orchestra_engine import narration_turn_events as n; \
open('tests/fixtures/orchestra/narration-turn.jsonl','w').write(''.join(e.to_json()+chr(10) for e in n()))" """
    path = os.path.join(os.path.dirname(__file__), "fixtures", "orchestra", "narration-turn.jsonl")
    assert [ev.from_json(line) for line in open(path, encoding="utf-8")] == narration_turn_events()
    kinds = [type(e).__name__ for e in narration_turn_events()]
    assert kinds.count("ThinkingText") == 1 and kinds.index("ToolCall") + 1 == kinds.index("Narration")
