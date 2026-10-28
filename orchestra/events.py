"""The event contract between the engine (orchestra/) and the face (ui/).

A backend turns whatever it drives (today the Claude Agent SDK; later sCoRE's own
streaming server) into these events, and the face draws only these. Neither side
imports the other. Events are plain frozen data so they round-trip through JSON:
`python -m orchestra.probe` prints them as JSON lines, and tests replay recorded
fixtures through a fake backend instead of calling a live model.

`organ` is LEAD ("rabble") for the lead session, otherwise a Crew persona slug.
Plan: RaBbLE-Grimoire/log/plans/sCoRE-RaBbLE-UI-Plan.md (event contract + mapping rules).
"""

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar, Literal

LEAD = "rabble"

Decision = Literal["allow", "allow_session", "deny"]
OrganStatus = Literal["completed", "failed", "killed"]
# "allowed" clears a warning the face is showing
LimitStatus = Literal["allowed", "warning", "rejected"]
# whether a finished turn answers the Observer, or was started by a background
# organ or a queued bus message
TurnOrigin = Literal["observer", "background"]

_KINDS: dict[str, type["Event"]] = {}


@dataclass(frozen=True, slots=True)
class Event:
    kind: ClassVar[str] = ""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls.kind = cls.__name__
        _KINDS[cls.kind] = cls

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.kind, **asdict(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def from_dict(data: dict[str, Any]) -> Event:
    """Rebuild an event from to_dict() output. Unknown keys are dropped so fixtures
    recorded before a field was removed still load."""
    data = dict(data)
    cls = _KINDS.get(data.pop("type", ""))
    if cls is None:
        raise ValueError(f"unknown event type in {data!r}")
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def from_json(line: str) -> Event:
    return from_dict(json.loads(line))


# ── session ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SessionStarted(Event):
    session_id: str
    model: str
    cwd: str


@dataclass(frozen=True, slots=True)
class SessionResumed(Event):
    session_id: str


# ── transcript ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TextDelta(Event):
    """Token-level streaming. Lead only: organs never stream deltas."""
    organ: str
    text: str


@dataclass(frozen=True, slots=True)
class TextBlock(Event):
    """A whole text block. For the lead it closes the deltas already shown (the face
    replaces them, it doesn't append); for organs it is the only text there is."""
    organ: str
    text: str


@dataclass(frozen=True, slots=True)
class Thinking(Event):
    """The model is thinking. Drawn as a pulse, never as content."""
    organ: str


@dataclass(frozen=True, slots=True)
class ToolCall(Event):
    organ: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult(Event):
    organ: str
    id: str
    ok: bool
    summary: str


# ── permissions ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PermissionAsk(Event):
    """A tool needs the Observer's yes. The face answers with backend.answer(id,
    PermissionReply). Asks can arrive concurrently, so the face queues them."""
    id: str
    organ: str
    tool: str
    input: dict[str, Any]
    title: str | None = None
    display_name: str | None = None
    description: str | None = None
    blocked_path: str | None = None
    suggestions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PermissionReply:
    """The face's answer to a PermissionAsk. Not an event: it flows face → engine."""
    decision: Decision
    reason: str = ""


# ── organs ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OrganStarted(Event):
    organ: str
    tool_use_id: str
    task_id: str | None
    brief: str
    context: bool = True  # False = a thin brief (no paths, short): an implicit handoff, entropy +0.25


@dataclass(frozen=True, slots=True)
class OrganProgress(Event):
    organ: str
    task_id: str
    usage: dict[str, Any] | None = None
    last_tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class OrganDone(Event):
    organ: str
    task_id: str
    status: OrganStatus
    result: str = ""


# ── state ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Usage(Event):
    """None means unchanged: the turn's result brings cost, a context check brings
    context_pct, and they arrive as separate events."""
    tokens: int
    cost: float | None = None
    context_pct: float | None = None


@dataclass(frozen=True, slots=True)
class RateLimited(Event):
    status: LimitStatus
    until: float | None = None  # unix seconds


@dataclass(frozen=True, slots=True)
class TurnDone(Event):
    stop_reason: str
    origin: TurnOrigin = "observer"


@dataclass(frozen=True, slots=True)
class ModelUsed(Event):
    """The lead's replies came from this model. Sent first and whenever it changes, so a
    silent fallback to another model is visible to the entropy score."""
    model: str


@dataclass(frozen=True, slots=True)
class EntropyChanged(Event):
    """Session entropy (orchestra/entropy.py): drives the entity's glitch and the trust profile.
    `reason` names what moved it; empty on the first report."""
    score: float
    band: str
    trust: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Error(Event):
    text: str
