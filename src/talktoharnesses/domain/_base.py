"""Shared Pydantic configuration and UTC helpers for domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, PlainSerializer

FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)


def require_utc(value: datetime | str) -> datetime:
    """Require a timezone-aware datetime (or ISO string) and normalize to UTC."""
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    else:
        parsed = value
    if parsed.tzinfo is None:
        msg = "timestamp must be timezone-aware UTC"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def _serialize_utc(value: datetime) -> str:
    return require_utc(value).isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    BeforeValidator(require_utc),
    PlainSerializer(_serialize_utc, return_type=str),
]

ConversationId = UUID
TurnId = UUID
EventId = UUID
CommandId = UUID
MessageId = UUID
InteractionId = UUID
ActivityId = UUID
BindingId = UUID
HarnessInstanceId = UUID
ToolId = UUID
ProcessId = UUID
