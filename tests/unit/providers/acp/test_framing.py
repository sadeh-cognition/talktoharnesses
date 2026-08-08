"""ACP newline framing unit tests."""

from __future__ import annotations

import pytest

from talktoharnesses.providers.acp.framing import FrameDecodeError, iter_json_frames


async def _collect(chunks: list[bytes]) -> list[dict[str, object]]:
    async def gen():
        for chunk in chunks:
            yield chunk

    return [frame async for frame in iter_json_frames(gen())]


@pytest.mark.asyncio
async def test_byte_at_a_time() -> None:
    line = b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n'
    frames = await _collect([bytes([b]) for b in line])
    assert len(frames) == 1
    assert frames[0]["method"] == "session/update"


@pytest.mark.asyncio
async def test_multi_frame_one_read() -> None:
    payload = b'{"jsonrpc":"2.0","id":1,"result":{}}\n{"jsonrpc":"2.0","method":"n","params":{}}\n'
    frames = await _collect([payload])
    assert len(frames) == 2


@pytest.mark.asyncio
async def test_empty_line_is_malformed() -> None:
    with pytest.raises(FrameDecodeError, match="empty"):
        await _collect([b"\n"])


@pytest.mark.asyncio
async def test_invalid_utf8() -> None:
    with pytest.raises(FrameDecodeError, match="UTF-8"):
        await _collect([b"\xff\n"])


@pytest.mark.asyncio
async def test_invalid_json() -> None:
    with pytest.raises(FrameDecodeError, match="JSON"):
        await _collect([b"not-json\n"])


@pytest.mark.asyncio
async def test_array_rejected() -> None:
    with pytest.raises(FrameDecodeError, match="object"):
        await _collect([b"[]\n"])


@pytest.mark.asyncio
async def test_unfinished_frame_at_eof_is_rejected() -> None:
    with pytest.raises(FrameDecodeError, match="truncated"):
        await _collect([b'{"jsonrpc":"2.0"'])
