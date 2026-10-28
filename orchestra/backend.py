"""Backends: what the face drives. One protocol, two implementations today.

- ClaudeCodeBackend: Claude Code headless through the Claude Agent SDK. One
  ClaudeSDKClient per session, pumped by one long-lived task that loops
  receive_messages() for the session's whole life (never receive_response(): that
  stops at the first ResultMessage and would drop turns started by background organs).
- ReplayBackend: replays recorded event JSON lines. Tests and face work use it;
  nothing in CI calls a live model.

A later sCoRE HTTP backend (plan U5) slots in behind the same protocol.
"""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any, Protocol

from . import events as ev
from . import trust
from .entropy import Entropy

_CLOSED = object()


class Backend(Protocol):
    async def start(self) -> None: ...
    def events(self) -> AsyncIterator[ev.Event]: ...
    async def send(self, text: str) -> None: ...
    async def interrupt(self) -> None: ...
    def answer(self, ask_id: str, reply: ev.PermissionReply) -> bool: ...
    async def set_model(self, model: str | None) -> None: ...
    async def set_permission_mode(self, mode: str) -> None: ...
    def set_trust(self, profile: str | None) -> None: ...
    async def stop_task(self, task_id: str) -> None: ...
    async def close(self) -> None: ...


class _Queued:
    """Shared event queue + events() iterator."""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()

    def emit(self, event: ev.Event) -> None:
        self._queue.put_nowait(event)

    def _finish(self) -> None:
        self._queue.put_nowait(_CLOSED)

    async def events(self) -> AsyncIterator[ev.Event]:
        while True:
            item = await self._queue.get()
            if item is _CLOSED:
                return
            yield item


# a command that runs the project's checks; a pass restores some of what compaction or a thin handoff cost
_TEST_RUN = re.compile(r"\b(pytest|py\.test|unittest|npm\s+(run\s+)?test|pnpm\s+test|yarn\s+test|cargo\s+test|go\s+test|make\s+(test|check)|ctest|tox)\b")


class ClaudeCodeBackend(_Queued):
    def __init__(self, cwd: str | Path, *, append: str | None = None, agents: dict[str, Any] | None = None,
                 model: str | None = None, permission_mode: str | None = None,
                 resume: str | None = None, continue_conversation: bool = False,
                 trust_override: str | None = None, entropy: Entropy | None = None):
        super().__init__()
        self.cwd = str(cwd)
        self.append = append
        self.agents = agents
        self.model = model
        self.permission_mode = permission_mode
        self.resume = resume
        self.continue_conversation = continue_conversation
        self.trust_override = trust_override  # None = what the session's entropy earns
        # a restart on the same session carries the score: restarting must not launder entropy
        self.entropy = entropy or Entropy(model)
        self._carried = entropy is not None
        self._lead_model: str | None = None
        self._reported: float | None = None
        self.client = None
        self._pump: asyncio.Task | None = None
        self._side: set[asyncio.Task] = set()
        self._calls: dict[str, tuple[str, dict]] = {}   # tool id -> (name, input), for re-grounding
        # agent_id (hooks, can_use_tool) → organ slug, filled by the SubagentStart hook
        self._organ_by_agent: dict[str, str] = {}

    def trust_profile(self) -> str:
        return self.trust_override if self.trust_override in trust.PROFILES else self.entropy.trust

    def _report_entropy(self, reason: str | None = None) -> None:
        """Emit when the score moved (or on the first report)."""
        score = self.entropy.score
        if self._reported is None or score != self._reported or reason:
            self._reported = score
            self.emit(ev.EntropyChanged(score, self.entropy.band, self.entropy.trust, reason or "",
                                        self.entropy.capability(), self.entropy.capabilities(), self.entropy.local))

    def _observe(self, event: ev.Event) -> None:
        """Feed what the stream shows into the entropy score."""
        reason = None
        reasons: list[str] = []
        if isinstance(event, (ev.SessionStarted, ev.SessionResumed)) and self._carried:
            self._carried = False
            reasons.append(self.entropy.handoff())
        if isinstance(event, ev.SessionStarted) and event.model:
            self.model = event.model  # the real id, so "default" gets its family's baseline
            self.entropy.observe_model(event.model)
        elif isinstance(event, ev.Usage) and event.context_pct is not None:
            reason = self.entropy.context(event.context_pct)
        elif isinstance(event, ev.ToolCall):
            if event.name == "Bash":
                self._calls[event.id] = (event.name, event.input)   # kept until its result says pass or fail
            if event.name == "Read" and event.input.get("file_path"):
                self.entropy.reground("read", str(event.input["file_path"]))   # re-loading what was lost
        elif isinstance(event, ev.ToolResult):
            name, tool_input = self._calls.pop(event.id, ("", {}))
            if event.ok and name == "Bash" and _TEST_RUN.search(str(tool_input.get("command", ""))):
                self.entropy.reground("verify")                                  # a passing test run
        elif isinstance(event, ev.RateLimited):
            reason = self.entropy.rate_limited(event.status)
        elif isinstance(event, ev.TurnDone) and event.origin == "observer":
            reason = self.entropy.turn_done()
        elif isinstance(event, ev.ModelUsed):
            reasons += self.entropy.model_used(event.model, self._lead_model)
            self._lead_model = event.model
            self.entropy.observe_model(event.model)
            self.model = event.model
        elif isinstance(event, ev.Error):
            reason = self.entropy.request_failed()
        elif isinstance(event, ev.OrganStarted):
            reason = self.entropy.organ_brief(event.organ, event.context)
        reasons += [reason] if reason else []
        self._report_entropy("; ".join(reasons) or None)

    def _auto_allow(self, tool: str, tool_input: dict[str, Any]) -> bool:
        return trust.auto_allow(self.trust_profile(), tool, tool_input, self.cwd)

    def organ_of_agent(self, agent_id: str | None) -> str:
        return self._organ_by_agent.get(agent_id, "agent") if agent_id else ev.LEAD

    def options(self):
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        from .permissions import Permissions
        self.permissions = Permissions(self.emit, self.organ_of_agent, self._auto_allow)
        system_prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
        if self.append:
            # an append to Claude Code's own prompt; replacing it drops its coding + safety guidance
            system_prompt["append"] = self.append
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            setting_sources=["user", "project", "local"],
            include_partial_messages=True,
            forward_subagent_text=True,
            cwd=self.cwd,
            model=self.model,
            permission_mode=self.permission_mode,
            resume=self.resume,
            continue_conversation=self.continue_conversation,
            agents=self.agents,
            can_use_tool=self.permissions.can_use_tool,
            hooks={"SubagentStart": [HookMatcher(hooks=[self._on_subagent_start])]},
        )

    async def _on_subagent_start(self, hook_input, tool_use_id, context):
        self._organ_by_agent[hook_input["agent_id"]] = hook_input.get("agent_type") or "agent"
        return {}

    async def start(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        from .sdk_map import SdkMapper
        self.mapper = SdkMapper(resumed=bool(self.resume or self.continue_conversation))
        self.client = ClaudeSDKClient(self.options())
        await self.client.connect()
        self._pump = asyncio.create_task(self._run())

    async def _run(self) -> None:
        from claude_agent_sdk import ResultMessage
        try:
            async for msg in self.client.receive_messages():
                for event in self.mapper.map(msg):
                    self.emit(event)
                    self._observe(event)
                if isinstance(msg, ResultMessage):
                    self._spawn(self._context_usage())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the face shows it; the session is over
            self.emit(ev.Error(f"engine: {e}"))
        finally:
            self._finish()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._side.add(task)
        task.add_done_callback(self._side.discard)

    async def _context_usage(self) -> None:
        # a control request; run beside the pump, which must keep reading
        with contextlib.suppress(Exception):
            usage = await self.client.get_context_usage()
            usage_event = ev.Usage(int(usage.get("totalTokens") or 0), None, float(usage.get("percentage") or 0))
            self.emit(usage_event)
            self._observe(usage_event)

    async def send(self, text: str) -> None:
        await self.client.query(text)

    async def interrupt(self) -> None:
        self.permissions.deny_all()
        await self.client.interrupt()

    def answer(self, ask_id: str, reply: ev.PermissionReply) -> bool:
        return self.permissions.answer(ask_id, reply)

    async def set_model(self, model: str | None) -> None:
        await self.client.set_model(model)
        self.model = model
        self._report_entropy(self.entropy.switch_model(model))

    async def set_permission_mode(self, mode: str) -> None:
        await self.client.set_permission_mode(mode)

    def set_trust(self, profile: str | None) -> None:
        self.trust_override = profile

    async def stop_task(self, task_id: str) -> None:
        await self.client.stop_task(task_id)

    async def close(self) -> None:
        if self.client is None:
            return
        self.permissions.deny_all("Session closed.")
        for task in list(self._side):
            task.cancel()
        with contextlib.suppress(Exception):
            await self.client.disconnect()
        if self._pump:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        self.client = None


class ReplayBackend(_Queued):
    """Plays recorded events. Each send() releases the next turn (up to and including
    a TurnDone); events before the first observer turn (session start) play at start().
    Calls the face makes are recorded in `calls` for tests."""

    def __init__(self, recorded: Iterable[ev.Event], delay: float = 0.0):
        super().__init__()
        self.delay = delay
        self.calls: list[tuple[str, Any]] = []
        self._turns: list[list[ev.Event]] = [[]]
        for event in recorded:
            self._turns[-1].append(event)
            if isinstance(event, (ev.SessionStarted, ev.SessionResumed, ev.TurnDone)):
                self._turns.append([])
        self._turns = [t for t in self._turns if t]
        self._player: asyncio.Task | None = None

    @classmethod
    def from_file(cls, path: str | Path, delay: float = 0.0) -> "ReplayBackend":
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return cls((ev.from_json(line) for line in lines if line.strip()), delay)

    def _next(self) -> None:
        if self._turns:
            turn = self._turns.pop(0)
            self._player = asyncio.create_task(self._play(turn, self._player))

    async def _play(self, turn: list[ev.Event], after: asyncio.Task | None) -> None:
        if after:
            await after
        for event in turn:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.emit(event)

    async def start(self) -> None:
        self.calls.append(("start", None))
        if self._turns and isinstance(self._turns[0][-1], (ev.SessionStarted, ev.SessionResumed)):
            self._next()

    async def send(self, text: str) -> None:
        self.calls.append(("send", text))
        self._next()

    async def interrupt(self) -> None:
        self.calls.append(("interrupt", None))

    def answer(self, ask_id: str, reply: ev.PermissionReply) -> bool:
        self.calls.append(("answer", (ask_id, reply)))
        return True

    async def set_model(self, model: str | None) -> None:
        self.calls.append(("set_model", model))

    async def set_permission_mode(self, mode: str) -> None:
        self.calls.append(("set_permission_mode", mode))

    def set_trust(self, profile: str | None) -> None:
        self.calls.append(("set_trust", profile))

    async def stop_task(self, task_id: str) -> None:
        self.calls.append(("stop_task", task_id))

    async def close(self) -> None:
        self.calls.append(("close", None))
        if self._player:
            await self._player
        self._finish()
