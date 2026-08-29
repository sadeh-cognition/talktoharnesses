"""Cursor ACP extension allowlists, config schemas, and validators.

Protocol-identical ACP v1 session/update and permission shapes are reused from
``schemas.base``. Cursor-only control notifications, configuration option
shapes, and future extension variants live here.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_cursor.acp.schemas.base import (
    is_allowlisted_permission_request,
    is_allowlisted_session_update,
)

# Captured Cursor control-plane notifications. Empty until fixtures prove them.
CURSOR_CONTROL_NOTIFICATIONS: frozenset[str] = frozenset()

# Cursor-only outbound methods beyond the shared ACP allowlist.
CURSOR_EXTRA_OUTBOUND_METHODS: frozenset[str] = frozenset({"session/set_config_option"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CursorQuestionOption(_Strict):
    id: str
    label: str


class CursorQuestion(_Strict):
    id: str
    prompt: str
    options: tuple[CursorQuestionOption, ...]
    allowMultiple: bool

    @field_validator("options", mode="before")
    @classmethod
    def _coerce_options_tuple(cls, value: object) -> object:
        return tuple(cast(list[object], value)) if isinstance(value, list) else value


class CursorAskQuestionParams(_Strict):
    toolCallId: str
    title: str | None = None
    questions: tuple[CursorQuestion, ...]

    @field_validator("questions", mode="before")
    @classmethod
    def _coerce_questions_tuple(cls, value: object) -> object:
        return tuple(cast(list[object], value)) if isinstance(value, list) else value


def is_allowlisted_cursor_ask_question(params: dict[str, Any] | None) -> bool:
    if params is None:
        return False
    try:
        CursorAskQuestionParams.model_validate(params)
    except ValidationError:
        return False
    return True


class _AckOnly(BaseModel):
    """Lenient base for extensions the adapter only acknowledges.

    ``cursor/update_todos`` is answered with ``{}`` regardless of payload, so
    rejecting an additive Cursor field (or a new todo status) would buy no
    safety while turning the request into UNSUPPORTED_NATIVE_EVENT — the same
    turn cancellation the ack exists to prevent. Interactions whose payloads
    are actually consumed (``cursor/ask_question``) stay strict.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class CursorTodoItem(_AckOnly):
    id: str
    content: str
    status: str


class CursorUpdateTodosParams(_AckOnly):
    toolCallId: str
    todos: tuple[CursorTodoItem, ...]
    merge: bool = False

    @field_validator("todos", mode="before")
    @classmethod
    def _coerce_todos_tuple(cls, value: object) -> object:
        return tuple(cast(list[object], value)) if isinstance(value, list) else value


def is_allowlisted_cursor_update_todos(params: dict[str, Any] | None) -> bool:
    if params is None:
        return False
    try:
        CursorUpdateTodosParams.model_validate(params)
    except ValidationError:
        return False
    return True


class CursorConfigOptionValue(_Strict):
    """One advertised value for a Cursor select configuration option."""

    name: str
    value: str
    description: str | None = None


class CursorSelectConfigOption(_Strict):
    """Cursor select-typed session configuration option (camelCase ACP shape)."""

    id: str
    category: str
    type: Literal["select"]
    currentValue: str
    options: tuple[CursorConfigOptionValue, ...]
    name: str | None = None
    description: str | None = None

    @field_validator("options", mode="before")
    @classmethod
    def _coerce_options_tuple(cls, value: object) -> object:
        # JSON arrays must become immutable tuples under strict validation.
        if isinstance(value, list):
            return tuple(cast(list[object], value))
        return value


def parse_cursor_config_options(result: object) -> tuple[CursorSelectConfigOption, ...]:
    """Strictly parse ``configOptions`` from a session/new, session/load, or setter result."""
    if not isinstance(result, dict):
        raise DomainError(
            ErrorCode.PROTOCOL_ERROR,
            "Cursor configuration result must be an object",
        )
    raw = cast(dict[object, object], cast(object, result))
    config_options = raw.get("configOptions")
    if not isinstance(config_options, list):
        raise DomainError(
            ErrorCode.PROTOCOL_ERROR,
            "Cursor configuration result missing configOptions list",
        )
    validated: list[CursorSelectConfigOption] = []
    for index, item in enumerate(cast(list[object], config_options)):
        try:
            validated.append(CursorSelectConfigOption.model_validate(item))
        except ValidationError as exc:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "malformed Cursor configuration option",
                details={"index": index},
            ) from exc
    return tuple(validated)


def is_allowlisted_cursor_session_update(params: dict[str, Any] | None) -> bool:
    return is_allowlisted_session_update(params)


def is_allowlisted_cursor_permission_request(params: dict[str, Any] | None) -> bool:
    return is_allowlisted_permission_request(params)
