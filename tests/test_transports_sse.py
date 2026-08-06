"""SSE helper tests using a fake response."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from talktoharnesses.errors import TransportError
from talktoharnesses.transports.sse import (
    aiter_sse_events,
    aiter_sse_json,
    parse_sse_frame,
)


class FakeResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


async def test_aiter_sse_json_basic() -> None:
    body = [
        "event: message",
        'data: {"type":"a","n":1}',
        "",
        'data: {"type":"b"}',
        "",
        "data: [DONE]",
        "",
    ]
    events = [e async for e in aiter_sse_json(FakeResponse(body))]
    assert events == [{"type": "a", "n": 1}, {"type": "b"}]


async def test_aiter_sse_json_multiline_data() -> None:
    body = [
        "data: {",
        'data: "x": 1',
        "data: }",
        "",
    ]
    # Joined as "{\n\"x\": 1\n}" which is valid JSON
    events = [e async for e in aiter_sse_json(FakeResponse(body))]
    assert events == [{"x": 1}]


async def test_aiter_sse_events_raw() -> None:
    body = [
        "event: tick",
        "data: hello",
        "",
        "data: world",
        "",
    ]
    pairs = [p async for p in aiter_sse_events(FakeResponse(body))]
    assert pairs == [("tick", "hello"), (None, "world")]


def test_parse_sse_frame() -> None:
    assert parse_sse_frame({"data": '{"ok": true}'}) == {"ok": True}
    assert parse_sse_frame({"data": "[DONE]"}) is None
    with pytest.raises(TransportError):
        parse_sse_frame({"data": "not-json"})
