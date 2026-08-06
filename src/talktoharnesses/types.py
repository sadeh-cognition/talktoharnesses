"""Shared input/output types for the Harness protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Opaque identifiers (string NewTypes keep call sites type-checked)
# ---------------------------------------------------------------------------

SessionId = NewType("SessionId", str)
ThreadId = NewType("ThreadId", str)
TurnId = NewType("TurnId", str)
ItemId = NewType("ItemId", str)
RequestId = NewType("RequestId", str)
EventId = NewType("EventId", str)

# ---------------------------------------------------------------------------
# Item / request discriminators (match T3 CanonicalItemType / CanonicalRequestType)
# ---------------------------------------------------------------------------

CanonicalItemType = Literal[
    "user_message",
    "assistant_message",
    "reasoning",
    "plan",
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "dynamic_tool_call",
    "collab_agent_tool_call",
    "web_search",
    "image_view",
    "review_entered",
    "review_exited",
    "context_compaction",
    "error",
    "unknown",
]

CanonicalRequestType = Literal[
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "dynamic_tool_call",
    "unknown",
]

ApprovalDecision = Literal["accept", "accept_for_session", "decline"]

# Capability support levels used across drivers.
SupportLevel = Literal["in-session", "unsupported"]


class Capabilities(BaseModel):
    """What a harness can do beyond the core turn loop."""

    model_config = ConfigDict(extra="forbid")

    session_model_switch: SupportLevel = "unsupported"
    interrupt_turn: SupportLevel = "in-session"
    approval: SupportLevel = "in-session"
    user_input: SupportLevel = "unsupported"
    resume_session: SupportLevel = "unsupported"


class SessionStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    resume: str | None = None
    """Provider-native session/thread id to resume, when supported."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendTurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    model: str | None = None
    """Per-turn model override when the harness supports in-session switch."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    thread_id: str | None = None
    provider: str
    model: str | None = None
    started_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str | None = None
    title: str
    status: str | None = None
    detail: str | None = None


class DiffHunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    patch: str | None = None
    additions: int | None = None
    deletions: int | None = None
