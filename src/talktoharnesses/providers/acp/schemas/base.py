"""Allowlisted ACP v1 methods and session/update variants."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


ALLOWED_OUTBOUND_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "session/new",
        "session/load",
        "session/prompt",
        "session/cancel",
    }
)

ALLOWED_INBOUND_METHODS: frozenset[str] = frozenset(
    {
        "session/update",
        "session/request_permission",
    }
)

GROK_CONTROL_NOTIFICATIONS: frozenset[str] = frozenset(
    {
        "_x.ai/mcp/servers_updated",
        "_x.ai/settings/update",
        "_x.ai/announcements/update",
    }
)

ALLOWED_SESSION_UPDATE_KINDS: frozenset[str] = frozenset(
    {
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "usage_update",
    }
)


def is_allowlisted_method(method: str, *, direction: str) -> bool:
    if direction == "outbound":
        return method in ALLOWED_OUTBOUND_METHODS
    if method in ALLOWED_INBOUND_METHODS:
        return True
    return method in GROK_CONTROL_NOTIFICATIONS


def is_allowlisted_session_update(params: dict[str, Any] | None) -> bool:
    if not params:
        return False
    try:
        SessionUpdateParams.model_validate(params)
    except ValueError:
        return False
    return True


class _TextUpdate(_Strict):
    sessionUpdate: Literal["agent_message_chunk", "agent_thought_chunk"]
    content: str | dict[str, Any] | None = None
    text: str | None = None
    messageId: str | None = None


class _ToolCallUpdate(_Strict):
    sessionUpdate: Literal["tool_call", "tool_call_update"]
    toolCallId: str
    title: str | None = None
    kind: str | None = None
    rawInput: Any = None
    arguments: Any = None
    status: str | None = None
    content: str | None = None
    error: str | None = None


class _PlanUpdate(_Strict):
    sessionUpdate: Literal["plan"]
    entries: list[dict[str, Any]] | None = None
    items: list[dict[str, Any]] | None = None


class _UsageUpdate(_Strict):
    sessionUpdate: Literal["usage_update"]
    inputTokens: int | None = None
    outputTokens: int | None = None
    totalTokens: int | None = None
    cachedInputTokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None


SessionUpdate = Annotated[
    _TextUpdate | _ToolCallUpdate | _PlanUpdate | _UsageUpdate,
    Field(discriminator="sessionUpdate"),
]


class SessionUpdateParams(_Strict):
    sessionId: str
    update: SessionUpdate


class InitializeParams(_Strict):
    protocolVersion: int
    clientInfo: dict[str, Any] = Field(default_factory=dict)
    clientCapabilities: dict[str, Any] = Field(default_factory=dict)


class SessionNewParams(_Strict):
    cwd: str
    mcpServers: list[Any] = Field(default_factory=list)


class SessionLoadParams(_Strict):
    sessionId: str
    cwd: str | None = None
    mcpServers: list[Any] = Field(default_factory=list)


class TextContentBlock(_Strict):
    type: str = "text"
    text: str


class SessionPromptParams(_Strict):
    sessionId: str
    prompt: list[TextContentBlock]


class SessionCancelParams(_Strict):
    sessionId: str
