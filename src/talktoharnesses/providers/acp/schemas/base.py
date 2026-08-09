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
        "_x.ai/models/update",
        "_x.ai/mcp_initialized",
    }
)

ALLOWED_SESSION_UPDATE_KINDS: frozenset[str] = frozenset(
    {
        "agent_message_chunk",
        "agent_thought_chunk",
        "user_message_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "usage_update",
        "session_info_update",
        "available_commands_update",
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


def is_allowlisted_permission_request(params: dict[str, Any] | None) -> bool:
    if params is None:
        return False
    try:
        PermissionRequestParams.model_validate(params)
    except ValueError:
        return False
    return True


class _TextUpdate(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    sessionUpdate: Literal["agent_message_chunk", "agent_thought_chunk", "user_message_chunk"]
    content: str | dict[str, Any] | None = None
    text: str | None = None
    messageId: str | None = None
    meta: Any | None = Field(default=None, alias="_meta")


class _ToolCallUpdate(_Strict):
    sessionUpdate: Literal["tool_call", "tool_call_update"]
    toolCallId: str
    title: str | None = None
    kind: str | None = None
    rawInput: Any = None
    rawOutput: Any = None
    arguments: Any = None
    status: str | None = None
    content: Any = None
    error: str | None = None
    locations: Any = None
    meta: Any | None = Field(default=None, alias="_meta")

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


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


class _SessionInfoUpdate(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    sessionUpdate: Literal["session_info_update"]
    title: str | None = None
    meta: Any | None = Field(default=None, alias="_meta")


class _AvailableCommandsUpdate(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    sessionUpdate: Literal["available_commands_update"]
    availableCommands: list[Any] | None = None
    meta: Any | None = Field(default=None, alias="_meta")


SessionUpdate = Annotated[
    _TextUpdate
    | _ToolCallUpdate
    | _PlanUpdate
    | _UsageUpdate
    | _SessionInfoUpdate
    | _AvailableCommandsUpdate,
    Field(discriminator="sessionUpdate"),
]


class SessionUpdateParams(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    sessionId: str
    update: SessionUpdate
    meta: Any | None = Field(default=None, alias="_meta")


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


class PermissionOption(_Strict):
    optionId: str
    kind: str
    name: str | None = None


class PermissionCommandInput(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    command: list[str] | str
    variant: str | None = None
    description: str | None = None
    is_background: bool | None = None
    meta: Any | None = Field(default=None, alias="_meta")


class PermissionFileInput(_Strict):
    path: str
    operation: Literal["read", "create", "modify", "delete"]


class PermissionNetworkInput(_Strict):
    """Explicit network-access marker for blanket network rules."""

    network: Literal[True] = True


PermissionToolInput = PermissionCommandInput | PermissionFileInput | PermissionNetworkInput


class PermissionToolCall(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    toolCallId: str | None = None
    title: str | None = None
    kind: str | None = None
    rawInput: PermissionToolInput | None = None
    status: str | None = None
    content: Any = None
    meta: Any | None = Field(default=None, alias="_meta")


def _permission_options() -> list[PermissionOption]:
    return []


class PermissionRequestParams(_Strict):
    sessionId: str | None = None
    toolCall: PermissionToolCall | None = None
    options: list[PermissionOption] = Field(default_factory=_permission_options)
    description: str | None = None
    summary: str | None = None
    # Explicit top-level network intent (fixture-proven field).
    networkAccess: bool | None = None
