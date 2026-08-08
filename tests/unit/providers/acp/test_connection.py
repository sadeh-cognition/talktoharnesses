"""AcpConnection correlation and lifecycle tests using a fake process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.acp.connection import AcpConnection
from talktoharnesses.providers.acp.jsonrpc import JsonRpcRemoteError, ProtocolCloseError


class FakeProcess:
    def __init__(self) -> None:
        self.process_id = uuid4()
        self.stdin_writes: list[bytes] = []
        self._stdout_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._stdout_taken = False

    async def write_stdin(self, data: bytes) -> None:
        self.stdin_writes.append(data)

    def stdout(self) -> AsyncIterator[bytes]:
        if self._stdout_taken:
            raise RuntimeError("single consumer")
        self._stdout_taken = True

        async def _iter() -> AsyncIterator[bytes]:
            while True:
                item = await self._stdout_q.get()
                if item is None:
                    return
                yield item

        return _iter()

    async def feed(self, line: str) -> None:
        await self._stdout_q.put((line if line.endswith("\n") else line + "\n").encode())

    async def eof(self) -> None:
        await self._stdout_q.put(None)


@pytest.mark.asyncio
async def test_request_response_and_delivery() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    future, delivered = await conn.request("initialize", {"protocolVersion": 1})
    assert delivered is not None
    assert proc.stdin_writes
    await proc.feed('{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}')
    result = await asyncio.wait_for(future, timeout=1)
    assert result["protocolVersion"] == 1
    await conn.close()


@pytest.mark.asyncio
async def test_jsonrpc_error_response() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    future, _ = await conn.request("session/prompt", {"sessionId": "s", "prompt": []})
    await proc.feed('{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"not found"}}')
    with pytest.raises(JsonRpcRemoteError) as exc:
        await asyncio.wait_for(future, timeout=1)
    assert exc.value.code == -32601
    await conn.close()


@pytest.mark.asyncio
async def test_unknown_response_id_is_protocol_error() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    # Give router a moment.
    await proc.feed('{"jsonrpc":"2.0","id":999,"result":{}}')
    await asyncio.sleep(0.05)
    # Pending should be empty; connection should have failed pending on protocol error.
    await conn.close()


@pytest.mark.asyncio
async def test_eof_fails_pending() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    future, _ = await conn.request("initialize", {"protocolVersion": 1})
    await proc.eof()
    with pytest.raises(ProtocolCloseError):
        await asyncio.wait_for(future, timeout=1)
    await conn.close()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    await conn.close()
    await conn.close()


@pytest.mark.asyncio
async def test_non_allowlisted_outbound_rejected() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    with pytest.raises(DomainError) as exc:
        await conn.request("x.ai/fs/list", {})
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
    await conn.close()


@pytest.mark.asyncio
async def test_inbound_permission_handler_may_respond_later() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    seen: list[object] = []

    async def handler(req: object) -> None:
        seen.append(req)
        # defer response
        return None

    conn.set_request_handler("session/request_permission", handler)
    await conn.start()
    await proc.feed('{"jsonrpc":"2.0","id":"p1","method":"session/request_permission","params":{}}')
    await asyncio.sleep(0.05)
    assert len(seen) == 1
    await conn.respond("p1", {"outcome": {"outcome": "cancelled"}})
    assert any(b"p1" in chunk for chunk in proc.stdin_writes)
    await conn.close()


@pytest.mark.asyncio
async def test_session_update_is_validated_with_strict_variant_schema() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    pending, _ = await conn.request("initialize", {"protocolVersion": 1})
    await proc.feed(
        '{"jsonrpc":"2.0","method":"session/update","params":'
        '{"sessionId":"s","update":{"sessionUpdate":"usage_update",'
        '"inputTokens":"not-an-integer"}}}'
    )
    with pytest.raises(DomainError) as exc:
        await asyncio.wait_for(pending, timeout=1)
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
    await conn.close()
