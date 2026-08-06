"""Canonical runtime event union.

Port of T3 Code's ``ProviderRuntimeEvent`` (subset for v1). Pydantic v2
discriminated union on ``type``. Every event carries ``raw`` so nothing is
lost when a normalizer does not recognize a field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from talktoharnesses.types import (
    CanonicalItemType,
    CanonicalRequestType,
    DiffHunk,
    PlanStep,
    TokenUsage,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_event_id() -> str:
    return str(uuid4())


class EventBase(BaseModel):
    """Fields shared by every runtime event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=_new_event_id)
    provider: str
    thread_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    turn_id: str | None = None
    item_id: str | None = None
    request_id: str | None = None
    raw: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SessionStarted(EventBase):
    type: Literal["session.started"] = "session.started"
    session_id: str
    model: str | None = None


class SessionConfigured(EventBase):
    type: Literal["session.configured"] = "session.configured"
    session_id: str
    model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class SessionExited(EventBase):
    type: Literal["session.exited"] = "session.exited"
    session_id: str
    reason: str | None = None
    exit_code: int | None = None


# ---------------------------------------------------------------------------
# Thread
# ---------------------------------------------------------------------------


class ThreadStarted(EventBase):
    type: Literal["thread.started"] = "thread.started"
    # thread_id is required for this event; kept optional on base for others.
    # Callers should treat it as present when type == thread.started.


class ThreadTokenUsageUpdated(EventBase):
    type: Literal["thread.token-usage.updated"] = "thread.token-usage.updated"
    usage: TokenUsage


# ---------------------------------------------------------------------------
# Turn
# ---------------------------------------------------------------------------


class TurnStarted(EventBase):
    type: Literal["turn.started"] = "turn.started"
    # turn_id expected


class TurnCompleted(EventBase):
    type: Literal["turn.completed"] = "turn.completed"
    stop_reason: str | None = None


class TurnAborted(EventBase):
    type: Literal["turn.aborted"] = "turn.aborted"
    reason: str | None = None


class TurnPlanUpdated(EventBase):
    type: Literal["turn.plan.updated"] = "turn.plan.updated"
    steps: list[PlanStep] = Field(default_factory=list)


class TurnDiffUpdated(EventBase):
    type: Literal["turn.diff.updated"] = "turn.diff.updated"
    hunks: list[DiffHunk] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class ItemStarted(EventBase):
    type: Literal["item.started"] = "item.started"
    item_type: CanonicalItemType
    title: str | None = None


class ItemUpdated(EventBase):
    type: Literal["item.updated"] = "item.updated"
    item_type: CanonicalItemType
    status: str | None = None
    detail: str | None = None


class ItemCompleted(EventBase):
    type: Literal["item.completed"] = "item.completed"
    item_type: CanonicalItemType
    status: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


class ContentDelta(EventBase):
    type: Literal["content.delta"] = "content.delta"
    text: str
    content_kind: Literal["text", "reasoning", "command_output"] = "text"


# ---------------------------------------------------------------------------
# Request (approvals)
# ---------------------------------------------------------------------------


class RequestOpened(EventBase):
    type: Literal["request.opened"] = "request.opened"
    request_type: CanonicalRequestType
    title: str | None = None
    detail: str | None = None
    tool_name: str | None = None
    # request_id expected


class RequestResolved(EventBase):
    type: Literal["request.resolved"] = "request.resolved"
    decision: str
    # request_id expected


# ---------------------------------------------------------------------------
# User input
# ---------------------------------------------------------------------------


class UserInputRequested(EventBase):
    type: Literal["user-input.requested"] = "user-input.requested"
    prompt: str | None = None
    questions: list[dict[str, Any]] = Field(default_factory=list)
    # request_id expected


class UserInputResolved(EventBase):
    type: Literal["user-input.resolved"] = "user-input.resolved"
    answers: dict[str, Any] = Field(default_factory=dict)
    # request_id expected


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class RuntimeWarning(EventBase):
    type: Literal["runtime.warning"] = "runtime.warning"
    message: str
    code: str | None = None


class RuntimeErrorEvent(EventBase):
    type: Literal["runtime.error"] = "runtime.error"
    message: str
    code: str | None = None


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

RuntimeEvent = Annotated[
    SessionStarted
    | SessionConfigured
    | SessionExited
    | ThreadStarted
    | ThreadTokenUsageUpdated
    | TurnStarted
    | TurnCompleted
    | TurnAborted
    | TurnPlanUpdated
    | TurnDiffUpdated
    | ItemStarted
    | ItemUpdated
    | ItemCompleted
    | ContentDelta
    | RequestOpened
    | RequestResolved
    | UserInputRequested
    | UserInputResolved
    | RuntimeWarning
    | RuntimeErrorEvent,
    Field(discriminator="type"),
]


_runtime_event_adapter: TypeAdapter[RuntimeEvent] | None = None


def _adapter() -> TypeAdapter[RuntimeEvent]:
    global _runtime_event_adapter
    if _runtime_event_adapter is None:
        _runtime_event_adapter = TypeAdapter(RuntimeEvent)
    return _runtime_event_adapter


def parse_runtime_event(data: dict[str, Any] | str | bytes) -> RuntimeEvent:
    """Parse a dict or JSON payload into a concrete RuntimeEvent subclass."""
    adapter = _adapter()
    if isinstance(data, dict):
        return adapter.validate_python(data)
    return adapter.validate_json(data)


def runtime_event_to_dict(event: RuntimeEvent) -> dict[str, Any]:
    """Serialize an event to a plain dict (JSON-compatible modes applied)."""
    # RuntimeEvent is a Union of BaseModel subclasses; cast for model_dump.
    return cast(BaseModel, event).model_dump(mode="json")
