"""Minimal async SSE client helpers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from talktoharnesses.errors import TransportError


async def aiter_sse_json(
    response: Any,
    *,
    data_only: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON objects from an SSE HTTP response body.

    ``response`` must be an ``httpx.Response`` opened with ``stream=True``
    (or anything exposing ``aiter_lines()``). Each ``data:`` line that is valid
    JSON is yielded as a dict. Non-JSON data lines are skipped when
    ``data_only`` is True; otherwise a ``TransportError`` is raised.
    """
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in response.aiter_lines():
        line: str = raw_line
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                event_name = None
                if payload in ("", "[DONE]"):
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    if data_only:
                        continue
                    raise TransportError(f"SSE data is not JSON: {payload!r}") from None
                if isinstance(obj, dict):
                    yield obj
            else:
                event_name = None
            continue

        if line.startswith(":"):
            # comment
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        # field: value — ignore unknown fields (id, retry, …)
        _ = event_name  # reserved for future event-name filtering

    # Flush trailing event without blank terminator
    if data_lines:
        payload = "\n".join(data_lines)
        if payload and payload != "[DONE]":
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                if not data_only:
                    raise TransportError(f"SSE data is not JSON: {payload!r}") from None
            else:
                if isinstance(obj, dict):
                    yield obj


async def aiter_sse_events(
    response: Any,
) -> AsyncIterator[tuple[str | None, str]]:
    """Yield ``(event_name, data)`` pairs from an SSE stream (raw data string)."""
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in response.aiter_lines():
        line: str = raw_line
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        yield event_name, "\n".join(data_lines)


def parse_sse_frame(frame: Mapping[str, str]) -> dict[str, Any] | None:
    """Parse a pre-split SSE frame mapping ``{"event": ..., "data": ...}``."""
    data = frame.get("data")
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        raise TransportError(f"SSE data is not JSON: {data!r}") from exc
    return obj if isinstance(obj, dict) else None
