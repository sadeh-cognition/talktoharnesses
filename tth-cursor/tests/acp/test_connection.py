"""AcpConnection correlation and lifecycle tests using a fake process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_cursor.acp.connection import AcpConnection
from tth_cursor.acp.jsonrpc import JsonRpcRemoteError, ProtocolCloseError


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
@pytest.mark.parametrize("response_id", [999, "1"])
@pytest.mark.parametrize(
    "response_body", ['"result":{}', '"error":{"code":-32603,"message":"stale error"}']
)
async def test_unmatched_response_does_not_fail_pending_request(
    response_id: int | str, response_body: str, caplog: pytest.LogCaptureFixture
) -> None:
    import json

    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    pending, _ = await conn.request("session/prompt", {"sessionId": "s", "prompt": []})
    await proc.feed('{"jsonrpc":"2.0","id":' + json.dumps(response_id) + "," + response_body + "}")
    await proc.feed('{"jsonrpc":"2.0","id":1,"result":{"stopReason":"end_turn"}}')
    assert await asyncio.wait_for(pending, timeout=1) == {"stopReason": "end_turn"}
    assert "Discarding ACP response with no pending request" in caplog.text
    await conn.close()


@pytest.mark.parametrize("retire", ["complete", "cancel_pending", "cancel_future"])
async def test_late_response_does_not_fail_next_request(
    retire: str, caplog: pytest.LogCaptureFixture
) -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()
    previous, _ = await conn.request("session/prompt", {"sessionId": "s", "prompt": []})
    if retire == "complete":
        await proc.feed('{"jsonrpc":"2.0","id":1,"result":{"stopReason":"end_turn"}}')
        await asyncio.wait_for(previous, timeout=1)
    elif retire == "cancel_pending":
        assert conn.cancel_pending(1)
    else:
        previous.cancel()
    pending, _ = await conn.request("session/prompt", {"sessionId": "s", "prompt": []})
    await proc.feed('{"jsonrpc":"2.0","id":1,"result":{"stopReason":"cancelled"}}')
    await proc.feed('{"jsonrpc":"2.0","id":2,"result":{"stopReason":"end_turn"}}')
    assert await asyncio.wait_for(pending, timeout=1) == {"stopReason": "end_turn"}
    # A reply the caller has abandoned reads differently from one whose id was
    # never sent: only the former still has a pending entry to abandon.
    expected = "an abandoned request" if retire == "cancel_future" else "no pending request"
    assert f"Discarding ACP response with {expected}" in caplog.text
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
async def test_permission_request_rejects_unallowlisted_action_fields() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]

    async def handler(_req: object) -> None:
        return None

    conn.set_request_handler("session/request_permission", handler)
    await conn.start()
    pending, _ = await conn.request("initialize", {"protocolVersion": 1})
    await proc.feed(
        '{"jsonrpc":"2.0","id":"p1","method":"session/request_permission",'
        '"params":{"network":true}}'
    )
    with pytest.raises(DomainError) as exc:
        await asyncio.wait_for(pending, timeout=1)
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
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


@pytest.mark.asyncio
async def test_notify_respond_error_cancel_and_frame_faults() -> None:
    proc = FakeProcess()
    conn = AcpConnection(proc)  # type: ignore[arg-type]
    await conn.start()

    with pytest.raises(DomainError):
        await conn.notify("not/allowlisted", {})
    await conn.notify("session/cancel", {"sessionId": "s"})
    await conn.respond_error("e1", -32000, "nope", data={"x": 1})
    assert any(b"e1" in chunk for chunk in proc.stdin_writes)

    future, _ = await conn.request("initialize", {"protocolVersion": 1})
    assert conn.cancel_pending(1) is True
    assert conn.cancel_pending(1) is False
    with pytest.raises(asyncio.CancelledError):
        await future

    # Malformed frame content → protocol fault closes writes.
    await proc.feed("{not-json\n")
    await asyncio.sleep(0.05)
    with pytest.raises(DomainError):
        await conn.notify("session/cancel", {"sessionId": "s"})
    await conn.close()


@pytest.mark.asyncio
async def test_inbound_request_without_handler_and_control_notification() -> None:
    from tth_cursor.acp.protocol import AcpProtocolConfig

    proc = FakeProcess()
    protocol = AcpProtocolConfig(
        outbound_methods=frozenset({"initialize"}),
        inbound_request_methods=frozenset({"session/request_permission"}),
        control_notifications=frozenset({"_x.ai/ping"}),
        request_validators={},
        session_update_validator=lambda params: True,
    )
    conn = AcpConnection(proc, protocol=protocol)  # type: ignore[arg-type]
    seen: list[str] = []

    async def control(note: object) -> None:
        seen.append(getattr(note, "method", ""))

    conn.set_notification_handler("_x.ai/ping", control)
    await conn.start()
    pending, _ = await conn.request("initialize", {"protocolVersion": 1})
    await proc.feed('{"jsonrpc":"2.0","method":"_x.ai/ping","params":{}}')
    await asyncio.sleep(0.02)
    assert seen == ["_x.ai/ping"]

    await proc.feed('{"jsonrpc":"2.0","id":"p1","method":"session/request_permission","params":{}}')
    with pytest.raises(DomainError) as exc:
        await asyncio.wait_for(pending, timeout=1)
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
    await conn.close()


@pytest.mark.asyncio
async def test_cursor_protocol_allows_set_config_option_grok_rejects() -> None:
    from tth_cursor.acp.protocol import cursor_acp_protocol, grok_acp_protocol

    cursor_proc = FakeProcess()
    cursor_conn = AcpConnection(cursor_proc, protocol=cursor_acp_protocol())  # type: ignore[arg-type]
    await cursor_conn.start()
    future, _ = await cursor_conn.request(
        "session/set_config_option",
        {"sessionId": "s", "configId": "mode", "value": "ask"},
    )
    await cursor_proc.feed('{"jsonrpc":"2.0","id":1,"result":{"configOptions":[]}}')
    result = await asyncio.wait_for(future, timeout=1)
    assert result == {"configOptions": []}
    await cursor_conn.close()

    grok_proc = FakeProcess()
    grok_conn = AcpConnection(grok_proc, protocol=grok_acp_protocol())  # type: ignore[arg-type]
    await grok_conn.start()
    with pytest.raises(DomainError) as exc:
        await grok_conn.request(
            "session/set_config_option",
            {"sessionId": "s", "configId": "mode", "value": "ask"},
        )
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
    await grok_conn.close()


@pytest.mark.asyncio
async def test_cursor_update_todos_request_is_acked() -> None:
    from tth_cursor.acp.protocol import cursor_acp_protocol

    proc = FakeProcess()
    conn = AcpConnection(proc, protocol=cursor_acp_protocol())  # type: ignore[arg-type]

    async def handler(_req: object) -> dict[str, object]:
        return {}

    conn.set_request_handler("cursor/update_todos", handler)
    await conn.start()
    await proc.feed(
        '{"jsonrpc":"2.0","id":"t1","method":"cursor/update_todos",'
        '"params":{"toolCallId":"tc-1","merge":false,"todos":['
        '{"id":"a","content":"Do the thing","status":"in_progress"}]}}'
    )
    await asyncio.sleep(0.05)
    assert any(b'"t1"' in chunk and b'"result"' in chunk for chunk in proc.stdin_writes)
    await conn.close()


@pytest.mark.asyncio
async def test_cursor_update_todos_rejects_unallowlisted_shape() -> None:
    from tth_cursor.acp.protocol import cursor_acp_protocol

    proc = FakeProcess()
    conn = AcpConnection(proc, protocol=cursor_acp_protocol())  # type: ignore[arg-type]

    async def handler(_req: object) -> dict[str, object]:
        return {}

    conn.set_request_handler("cursor/update_todos", handler)
    await conn.start()
    pending, _ = await conn.request("initialize", {"protocolVersion": 1})
    await proc.feed(
        '{"jsonrpc":"2.0","id":"t2","method":"cursor/update_todos",'
        '"params":{"toolCallId":"tc-1","todos":[{"id":"a","status":"pending"}]}}'
    )
    with pytest.raises(DomainError) as exc:
        await asyncio.wait_for(pending, timeout=1)
    assert exc.value.code is ErrorCode.UNSUPPORTED_NATIVE_EVENT
    await conn.close()
