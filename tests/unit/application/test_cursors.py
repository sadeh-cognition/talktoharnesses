"""Opaque keyset cursor contract."""

from __future__ import annotations

from uuid import uuid4

import pytest

from talktoharnesses.application.cursors import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    clamp_page_limit,
    decode_cursor,
    decode_search_cursor,
    encode_cursor,
    encode_search_cursor,
)
from talktoharnesses.domain import DomainError, ErrorCode


def test_round_trip() -> None:
    item_id = uuid4()
    cursor = encode_cursor(sort="2026-08-08T12:00:00+00:00", id=item_id)
    sort, decoded = decode_cursor(cursor)
    assert sort == "2026-08-08T12:00:00+00:00"
    assert decoded == item_id


def test_invalid_cursor_raises() -> None:
    with pytest.raises(DomainError) as exc:
        decode_cursor("not-a-cursor!!!")
    assert exc.value.code is ErrorCode.INVALID_CURSOR


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "2026-08-08T12:00:00"])
def test_search_cursor_rejects_invalid_or_naive_timestamp(timestamp: str) -> None:
    digest = "query-digest"
    cursor = encode_search_cursor(rank=1, updated_at=timestamp, id=uuid4(), digest=digest)
    with pytest.raises(DomainError) as exc:
        decode_search_cursor(cursor, digest=digest)
    assert exc.value.code is ErrorCode.INVALID_CURSOR


def test_clamp_page_limit() -> None:
    assert clamp_page_limit(None) == DEFAULT_PAGE_SIZE
    assert clamp_page_limit(1) == 1
    assert clamp_page_limit(MAX_PAGE_SIZE) == MAX_PAGE_SIZE
    with pytest.raises(DomainError):
        clamp_page_limit(0)
    with pytest.raises(DomainError):
        clamp_page_limit(MAX_PAGE_SIZE + 1)
