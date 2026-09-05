"""Grok extension notification schemas (strict decode, then ignore for transcript)."""

from __future__ import annotations

from typing import Any, Literal

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
#
# ``Bash`` already matches the protocol-level ``PermissionCommandInput``. The
# two edit shapes are Grok-specific and live here, not in ``schemas.base``,
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


GrokPermissionToolInput = (
    PermissionToolInput | GrokWritePermissionInput | GrokSearchReplacePermissionInput
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
