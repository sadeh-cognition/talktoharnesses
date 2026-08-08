"""Opaque keyset cursor encode/decode shared by facade list queries."""

from __future__ import annotations

import base64
import json
from typing import cast
from uuid import UUID

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def clamp_page_limit(limit: int | None) -> int:
    """Normalize a requested page size to the Phase 5 contract bounds."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "page limit must be between 1 and 200",
            details={"limit": limit},
        )
    return limit


def encode_cursor(*, sort: str, id: UUID) -> str:
    """Encode an opaque, versionless cursor from a sort value and UUID tie-breaker."""
    payload = {"s": sort, "i": str(id)}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, UUID]:
    """Decode a cursor. Invalid values raise ``invalid_cursor`` (no first-page fallback)."""
    if not cursor:
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data: object = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor") from exc
    if not isinstance(data, dict):
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor")
    payload = cast(dict[str, object], data)
    sort = payload.get("s")
    raw_id = payload.get("i")
    if not isinstance(sort, str) or not isinstance(raw_id, str):
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor")
    try:
        item_id = UUID(raw_id)
    except ValueError as exc:
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor") from exc
    return sort, item_id
