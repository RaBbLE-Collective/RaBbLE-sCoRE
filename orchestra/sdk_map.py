"""Claude Agent SDK messages → orchestra events (the mapping rules in the plan).

Pure and synchronous: feed it SDK message objects, get events back. The backend
owns the client; this owns the bookkeeping that turns one flat stream into organs.

- Text: with include_partial_messages the lead gets token deltas and then the whole
  AssistantMessage anyway; we pass both and the face replaces the deltas with the
  TextBlock. Organs only ever get TextBlocks.
- Organ identity: an Agent/Task tool_use from the lead names the organ
  (`subagent_type`); every later message tagged with that tool_use id as
  `parent_tool_use_id` belongs to it. TaskStartedMessage ties the tool_use id to the
  task id that stop_task takes.
- Organ end: a terminal status from TaskNotificationMessage or TaskUpdatedMessage,
  or (foreground organs) the Agent tool's own result, whichever comes first.
"""

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    UserMessage,
)
from claude_agent_sdk import TextBlock as SdkText
from claude_agent_sdk import ThinkingBlock, ToolResultBlock, ToolUseBlock

from . import events as ev

AGENT_TOOLS = {"Agent", "Task"}
SUMMARY_CHARS = 200
BRIEF_CHARS = 80
_TERMINAL = {"completed": "completed", "failed": "failed", "stopped": "killed", "killed": "killed"}
_LIMITS = {"allowed": "allowed", "allowed_warning": "warning", "rejected": "rejected"}


def summarize(content: Any) -> str:
    """One line out of a tool result (str, or a list of content blocks)."""
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    text = " ".join(str(content or "").split())
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 1] + "…"


def _brief(tool_input: dict[str, Any]) -> str:
    text = tool_input.get("description") or tool_input.get("prompt") or ""
    text = " ".join(str(text).split())
    return text if len(text) <= BRIEF_CHARS else text[:BRIEF_CHARS - 1] + "…"


def _tokens(usage: dict[str, Any] | None) -> int:
    if not usage:
        return 0
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return sum(int(usage.get(k) or 0) for k in keys)


class SdkMapper:
    def __init__(self, resumed: bool = False):
        self.resumed = resumed
        self.session_id: str | None = None
        self.organ_by_tool: dict[str, str] = {}   # Agent tool_use id → organ slug
        self.tool_by_task: dict[str, str] = {}    # task id → Agent tool_use id
        self.task_by_tool: dict[str, str] = {}
        self.done: set[str] = set()               # Agent tool_use ids already ended
        self.last_text: dict[str, str] = {}       # Agent tool_use id → organ's latest text
        self.lead_thinking = False

    def organ_for(self, parent_tool_use_id: str | None) -> str:
        if parent_tool_use_id is None:
            return ev.LEAD
        return self.organ_by_tool.get(parent_tool_use_id, "agent")

    def _organ_task(self, task_id: str, tool_use_id: str | None) -> tuple[str, str] | None:
        """(organ, tool_use_id) when a task is an organ, None for other background work."""
        tool = tool_use_id or self.tool_by_task.get(task_id)
        if tool in self.organ_by_tool:
            return self.organ_by_tool[tool], tool
        return None

    def _end(self, tool: str, organ: str, task_id: str | None, status: str, result: str) -> list[ev.Event]:
        if tool in self.done:
            return []
        self.done.add(tool)
        result = result or self.last_text.get(tool, "")
        return [ev.OrganDone(organ, task_id or self.task_by_tool.get(tool, ""), status, result)]

    def map(self, msg: Any) -> list[ev.Event]:
        # Task* are SystemMessage subclasses: match them first
        if isinstance(msg, TaskStartedMessage):
            if msg.tool_use_id:
                self.tool_by_task[msg.task_id] = msg.tool_use_id
                self.task_by_tool[msg.tool_use_id] = msg.task_id
            return []
        if isinstance(msg, TaskProgressMessage):
            hit = self._organ_task(msg.task_id, msg.tool_use_id)
            if not hit:
                return []
            return [ev.OrganProgress(hit[0], msg.task_id, dict(msg.usage or {}), msg.last_tool_name)]
        if isinstance(msg, (TaskNotificationMessage, TaskUpdatedMessage)):
            status = _TERMINAL.get(msg.status or "")
            hit = self._organ_task(msg.task_id, getattr(msg, "tool_use_id", None))
            if not (status and hit):
                return []
            return self._end(hit[1], hit[0], msg.task_id, status, getattr(msg, "summary", "") or "")
        if isinstance(msg, SystemMessage):
            return self._system(msg)
        if isinstance(msg, StreamEvent):
            return self._stream(msg)
        if isinstance(msg, AssistantMessage):
            return self._assistant(msg)
        if isinstance(msg, UserMessage):
            return self._user(msg)
        if isinstance(msg, ResultMessage):
            return self._result(msg)
        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            status = _LIMITS.get(info.status)
            return [ev.RateLimited(status, float(info.resets_at) if info.resets_at else None)] if status else []
        return []

    def _system(self, msg: SystemMessage) -> list[ev.Event]:
        # init arrives at the start of every turn; only the first one opens the session
        if msg.subtype != "init" or self.session_id is not None:
            return []
        data = msg.data
        self.session_id = data.get("session_id", "")
        if self.resumed:
            return [ev.SessionResumed(self.session_id)]
        return [ev.SessionStarted(self.session_id, data.get("model", ""), data.get("cwd", ""))]

    def _stream(self, msg: StreamEvent) -> list[ev.Event]:
        if msg.parent_tool_use_id is not None:
            return []
        e = msg.event
        kind = e.get("type")
        if kind == "content_block_start":
            self.lead_thinking = (e.get("content_block") or {}).get("type") == "thinking"
            return [ev.Thinking(ev.LEAD)] if self.lead_thinking else []
        if kind == "content_block_delta":
            delta = e.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [ev.TextDelta(ev.LEAD, delta["text"])]
        return []

    def _assistant(self, msg: AssistantMessage) -> list[ev.Event]:
        organ = self.organ_for(msg.parent_tool_use_id)
        out: list[ev.Event] = []
        if msg.error:
            out.append(ev.Error(f"{organ}: {msg.error}"))
        for block in msg.content:
            if isinstance(block, SdkText):
                if block.text:
                    out.append(ev.TextBlock(organ, block.text))
                    if msg.parent_tool_use_id:
                        self.last_text[msg.parent_tool_use_id] = block.text
            elif isinstance(block, ThinkingBlock):
                if organ != ev.LEAD:  # the lead's thinking already pulsed from the stream
                    out.append(ev.Thinking(organ))
            elif isinstance(block, ToolUseBlock):
                out.append(ev.ToolCall(organ, block.id, block.name, block.input))
                if block.name in AGENT_TOOLS:
                    slug = str(block.input.get("subagent_type") or "agent")
                    self.organ_by_tool[block.id] = slug
                    out.append(ev.OrganStarted(slug, block.id, None, _brief(block.input)))
        return out

    def _user(self, msg: UserMessage) -> list[ev.Event]:
        if isinstance(msg.content, str):
            return []
        organ = self.organ_for(msg.parent_tool_use_id)
        out: list[ev.Event] = []
        for block in msg.content:
            if not isinstance(block, ToolResultBlock):
                continue
            ok = not block.is_error
            summary = summarize(block.content)
            out.append(ev.ToolResult(organ, block.tool_use_id, ok, summary))
            if block.tool_use_id in self.organ_by_tool:
                slug = self.organ_by_tool[block.tool_use_id]
                out += self._end(block.tool_use_id, slug, None, "completed" if ok else "failed", summary)
        return out

    def _result(self, msg: ResultMessage) -> list[ev.Event]:
        out: list[ev.Event] = []
        if msg.is_error:
            detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
            out.append(ev.Error(detail))
        out.append(ev.Usage(_tokens(msg.usage), msg.total_cost_usd))
        origin = msg.origin
        observer = origin is None or origin.get("kind") == "human"
        out.append(ev.TurnDone(msg.stop_reason or msg.subtype, "observer" if observer else "background"))
        return out
