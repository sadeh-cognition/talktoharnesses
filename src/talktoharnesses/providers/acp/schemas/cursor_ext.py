"""Cursor ACP extension allowlists, config schemas, and validators.

Protocol-identical ACP v1 session/update and permission shapes are reused from
``schemas.base``. Cursor-only control notifications, configuration option
shapes, and future extension variants live here.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.acp.schemas.base import (
    is_allowlisted_permission_request,
    is_allowlisted_session_update,
)

# Captured Cursor control-plane notifications. Empty until fixtures prove them.
CURSOR_CONTROL_NOTIFICATIONS: frozenset[str] = frozenset()

# Cursor-only outbound methods beyond the shared ACP allowlist.
CURSOR_EXTRA_OUTBOUND_METHODS: frozenset[str] = frozenset({"session/set_config_option"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


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
