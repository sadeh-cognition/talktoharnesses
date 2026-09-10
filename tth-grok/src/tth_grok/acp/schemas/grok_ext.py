"""Grok extension notification schemas (strict decode, then ignore for transcript)."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from tth_grok.acp.schemas.base import (
    PermissionRequestParams,
    PermissionToolCall,
    PermissionToolInput,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GrokControlNotification(_Strict):
    """Loose retention model for allowlisted control-plane notifications.

    Fields beyond method are retained as a raw params dict after envelope
    validation; unknown *methods* are rejected at the connection layer.
    """

    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class GrokQuestionOption(_Strict):
    label: str
    description: str | None = None


class GrokQuestion(_Strict):
    id: str | None = None
    question: str
    options: list[GrokQuestionOption]
    multiSelect: bool | None = None


class GrokAskUserQuestionParams(_Strict):
    sessionId: str
    toolCallId: str
    questions: list[GrokQuestion]
    mode: str | None = None


def is_allowlisted_ask_user_question(params: dict[str, Any] | None) -> bool:
    if params is None:
        return False
    try:
        GrokAskUserQuestionParams.model_validate(params)
    except ValueError:
        return False
    return True


# --- session/request_permission: Grok-native tool inputs -----------------------
#
# Grok 1.0.13 (5e9a58528b76) forwards its own tool arguments as ``rawInput``
# on permission requests, tagged with a ``variant``. Captured shapes:
#
#   {"variant": "Write", "file_path": ..., "content": ...}
#   {"variant": "SearchReplace", "file_path": ..., "old_string": ...,
#    "new_string": ..., "replace_all": false}
#   {"variant": "Bash", "command": ..., "description": ..., "is_background": false}
#   {"variant": "UseTool", "tool_name": "<server>__<tool>", "tool_input": {...}}
#
# ``Bash`` already matches the protocol-level ``PermissionCommandInput``. The
# edit and MCP shapes are Grok-specific and live here, not in ``schemas.base``,
# which stays protocol-identical across the ACP adapters. Extend this list only
# from captured fixtures (see tests/harness/test_permission_fixtures.py).


class _StrictAliased(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class GrokWritePermissionInput(_StrictAliased):
    """``write`` tool (namespace ``opencode``): create or overwrite a file."""

    variant: Literal["Write"]
    file_path: str
    content: str
    meta: Any | None = Field(default=None, alias="_meta")


class GrokSearchReplacePermissionInput(_StrictAliased):
    """``search_replace`` tool (namespace ``grok_build``): edit a file in place."""

    variant: Literal["SearchReplace"]
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool | None = None
    meta: Any | None = Field(default=None, alias="_meta")


class GrokUseToolPermissionInput(_StrictAliased):
    """``use_tool`` tool (namespace ``grok_build``): call an MCP server tool.

    Grok names MCP tools ``<server>__<tool>`` and forwards the call arguments
    untouched as ``tool_input``. Captured on 1.0.13 in both permission modes
    (``--permission-mode default`` and ``--always-approve``), with
    ``toolCall.kind == "other"`` and ``toolCall.title`` equal to ``tool_name``.
    """

    variant: Literal["UseTool"]
    tool_name: str
    tool_input: dict[str, Any]
    meta: Any | None = Field(default=None, alias="_meta")


class GrokWebFetchPermissionInput(_StrictAliased):
    """Grok's web fetch permission input, observed on 2026-09-09."""

    variant: Literal["WebFetch"]
    url: str
    meta: Any | None = Field(default=None, alias="_meta")


GrokPermissionToolInput = (
    PermissionToolInput
    | GrokWritePermissionInput
    | GrokSearchReplacePermissionInput
    | GrokUseToolPermissionInput
    | GrokWebFetchPermissionInput
)


class GrokPermissionToolCall(PermissionToolCall):
    rawInput: GrokPermissionToolInput | None = None  # pyright: ignore[reportIncompatibleVariableOverride]


class GrokPermissionRequestParams(PermissionRequestParams):
    toolCall: GrokPermissionToolCall | None = None  # pyright: ignore[reportIncompatibleVariableOverride]


def is_allowlisted_grok_permission_request(params: dict[str, Any] | None) -> bool:
    """ACP v1 permission shape plus the captured Grok edit-tool inputs."""
    if params is None:
        return False
    try:
        GrokPermissionRequestParams.model_validate(params)
    except ValueError:
        return False
    return True


def grok_file_permission_target(raw_input: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(path, operation)`` for a captured Grok edit-tool ``rawInput``.

    Only the ``variant`` tag is consulted, never titles or summaries. ``Write``
    creates or overwrites, so it is reported as ``create``; ``SearchReplace``
    edits existing content and is reported as ``modify``.
    """
    variant = raw_input.get("variant")
    path = raw_input.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    if variant == "Write":
        return path, "create"
    if variant == "SearchReplace":
        return path, "modify"
    return None


def grok_is_network_permission(raw_input: dict[str, Any]) -> bool:
    """Whether a captured Grok ``rawInput`` asks to reach the network.

    Only the ``variant`` tag is consulted, never titles or summaries.
    ``WebFetch`` retrieves a URL, which is the network approval the canonical
    action models.
    """
    return raw_input.get("variant") == "WebFetch"


MCP_TOOL_NAME_PREFIX = "mcp__"


def grok_mcp_permission_tool_name(raw_input: dict[str, Any]) -> str | None:
    """Return the fully qualified MCP tool name for a captured ``UseTool`` input.

    Grok's ``tool_name`` is already ``<server>__<tool>``; prefixing ``mcp__``
    yields the ``mcp__<server>__<tool>`` form the other adapters report for MCP
    approvals, so callers can recognise calls to the servers they attached
    without a Grok-specific rule. Only the ``variant`` tag is consulted.
    """
    if raw_input.get("variant") != "UseTool":
        return None
    return _qualified_mcp_tool_name(raw_input.get("tool_name"))


def grok_mcp_tool_call_name(update: dict[str, Any]) -> str | None:
    """Return the fully qualified MCP tool name for a ``use_tool`` tool call.

    Grok reports an MCP call as a call to its own ``use_tool`` tool, with the
    target in ``rawInput.tool_name``. The first ``tool_call`` frame is titled
    ``use_tool`` and its ``rawInput`` has no ``variant`` yet; later
    ``tool_call_update`` frames carry ``variant: UseTool`` and retitle the
    call to the target. Any of those markers identifies the wrapper; the
    ``_meta`` tool descriptor is consulted as well because it names the tool
    independently of the title.
    """
    raw_input = update.get("rawInput")
    if not isinstance(raw_input, dict):
        return None
    raw_map = cast(dict[str, Any], raw_input)
    is_use_tool: bool = (
        raw_map.get("variant") == "UseTool"
        or update.get("title") == "use_tool"
        or _meta_tool_name(update.get("_meta")) == "use_tool"
    )
    if not is_use_tool:
        return None
    return _qualified_mcp_tool_name(raw_map.get("tool_name"))


def _meta_tool_name(meta: object) -> str | None:
    """``_meta["x.ai/tool"]["name"]`` when present, else ``None``."""
    if not isinstance(meta, dict):
        return None
    tool = cast(dict[str, Any], meta).get("x.ai/tool")
    if not isinstance(tool, dict):
        return None
    name = cast(dict[str, Any], tool).get("name")
    return name if isinstance(name, str) else None


def _qualified_mcp_tool_name(tool_name: object) -> str | None:
    if not isinstance(tool_name, str) or not tool_name:
        return None
    return f"{MCP_TOOL_NAME_PREFIX}{tool_name}"
