"""Permission prompts: the SDK's can_use_tool callback → PermissionAsk → the face's answer.

Each ask parks on an asyncio.Future until the face calls answer(). Asks arrive
concurrently (the SDK runs each callback as its own task), so the face queues them.
"Allow for session" never writes to disk: every rule is forced to destination
"session", even when the CLI's own suggestion names localSettings/projectSettings.

can_use_tool only fires when Claude Code would otherwise prompt: allow rules (a
project's `Read(*)`), acceptEdits for edits, and bypassPermissions all skip it.
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, PermissionUpdate
from claude_agent_sdk.types import PermissionRuleValue

from . import events as ev

DENY_DEFAULT = "The Observer declined."


def session_rules(tool: str, tool_input: dict[str, Any], suggestions: list[PermissionUpdate]) -> list[PermissionUpdate]:
    """The CLI's addRules suggestions pinned to this session; else a narrow rule of our own."""
    picked = [s for s in suggestions if s.type == "addRules" and s.rules]
    if picked:
        return [PermissionUpdate(type="addRules", rules=s.rules, behavior=s.behavior or "allow",
                                 destination="session") for s in picked]
    # no suggestion: Bash gets the exact command, never all of Bash
    content = tool_input.get("command") if tool == "Bash" else None
    return [PermissionUpdate(type="addRules", rules=[PermissionRuleValue(tool, content)],
                             behavior="allow", destination="session")]


def _suggestion_dict(s: PermissionUpdate) -> dict[str, Any]:
    d = s.to_dict() if hasattr(s, "to_dict") else dict(vars(s))
    return {k: v for k, v in d.items() if v is not None}


class Permissions:
    def __init__(self, emit: Callable[[ev.Event], None], organ_of_agent: Callable[[str | None], str]):
        self._emit = emit
        self._organ_of_agent = organ_of_agent
        self._pending: dict[str, tuple[asyncio.Future, str, dict[str, Any], list[PermissionUpdate]]] = {}

    @property
    def pending(self) -> list[str]:
        return list(self._pending)

    async def can_use_tool(self, tool: str, tool_input: dict[str, Any], context: Any):
        ask_id = context.tool_use_id or uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        suggestions = list(context.suggestions or [])
        self._pending[ask_id] = (future, tool, tool_input, suggestions)
        self._emit(ev.PermissionAsk(
            ask_id, self._organ_of_agent(context.agent_id), tool, tool_input,
            title=context.title, display_name=context.display_name, description=context.description,
            blocked_path=context.blocked_path, suggestions=[_suggestion_dict(s) for s in suggestions],
        ))
        try:
            reply: ev.PermissionReply = await future
        finally:
            self._pending.pop(ask_id, None)
        if reply.decision == "allow":
            return PermissionResultAllow()
        if reply.decision == "allow_session":
            return PermissionResultAllow(updated_permissions=session_rules(tool, tool_input, suggestions))
        return PermissionResultDeny(message=reply.reason or DENY_DEFAULT)

    def answer(self, ask_id: str, reply: ev.PermissionReply) -> bool:
        entry = self._pending.get(ask_id)
        if not entry or entry[0].done():
            return False
        entry[0].set_result(reply)
        return True

    def deny_all(self, reason: str = "Interrupted.") -> None:
        """On interrupt or close, nothing is left hanging."""
        for future, *_ in list(self._pending.values()):
            if not future.done():
                future.set_result(ev.PermissionReply("deny", reason))
