"""Newline-delimited JSON framing over a raw stdout byte stream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


class FrameDecodeError(Exception):
    """Malformed ACP frame (empty line, invalid UTF-8, or invalid JSON)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def iter_json_frames(stdout: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    """Yield complete JSON objects from newline-delimited stdout chunks.

    A single read may contain a partial frame, one frame, or several frames.
    An empty line is a malformed frame. Arrays are rejected by callers after
    decode; this generator only produces decoded Python objects.
    """
    buffer = bytearray()
    async for chunk in stdout:
        if not chunk:
            continue
        buffer.extend(chunk)
        while True:
            nl = buffer.find(b"\n")
            if nl < 0:
                break
            line = bytes(buffer[:nl])
            del buffer[: nl + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                raise FrameDecodeError("empty ACP frame")
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FrameDecodeError("invalid UTF-8 in ACP frame") from exc
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FrameDecodeError("invalid JSON in ACP frame") from exc
            if not isinstance(obj, dict):
                raise FrameDecodeError("ACP frame must be a JSON object")
            yield obj
    if buffer:
        raise FrameDecodeError("truncated ACP frame at EOF")
