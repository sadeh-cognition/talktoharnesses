"""stdio JSON-RPC peer tests against the echo mock."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from talktoharnesses.errors import ProtocolError, TransportError
from talktoharnesses.transports.process import spawn_process
from talktoharnesses.transports.stdio_jsonrpc import JsonRpcPeer

FIXTURE = Path(__file__).parent / "fixtures" / "echo_jsonrpc_peer.py"


async def _open_echo_peer() -> tuple[JsonRpcPeer, object]:
    proc = await spawn_process([sys.executable, str(FIXTURE)])
    assert proc.stdout is not None and proc.stdin is not None
    peer = JsonRpcPeer(reader=proc.stdout, writer=proc.stdin)
    peer.start()
    return peer, proc


async def test_request_response_echo() -> None:
    peer, proc = await _open_echo_peer()
    try:
        result = await peer.request("hello", {"x": 1})
        assert result == {"echo": {"x": 1}, "method": "hello"}
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)  # type: ignore[attr-defined]


async def test_notification_fanout() -> None:
    peer, proc = await _open_echo_peer()
    received: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    async def on_note(method: str, params: object) -> None:
        await received.put((method, params))

    peer.on_notification(on_note)
    try:
        await peer.request("notify-me", {"k": "v"})
        method, params = await asyncio.wait_for(received.get(), timeout=2.0)
        assert method == "peer/tick"
        assert params == {"from": "echo", "payload": {"k": "v"}}
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)  # type: ignore[attr-defined]


async def test_inbound_request_handler() -> None:
    peer, proc = await _open_echo_peer()
    received: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    async def handle_ping(method: str, params: object) -> dict[str, object]:
        assert method == "client/ping"
        return {"pong": True, "params": params}

    async def on_note(method: str, params: object) -> None:
        await received.put((method, params))

    peer.on_request("client/ping", handle_ping)
    peer.on_notification(on_note)
    try:
        await peer.request("ping-client", {})
        method, params = await asyncio.wait_for(received.get(), timeout=2.0)
        assert method == "peer/ping-result"
        assert params == {"result": {"pong": True, "params": {"n": 1}}, "error": None}
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)  # type: ignore[attr-defined]


async def test_request_timeout() -> None:
    peer, proc = await _open_echo_peer()
    try:
        with pytest.raises(TimeoutError):
            await peer.request("hang", {}, timeout=0.3)
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)  # type: ignore[attr-defined]


async def test_peer_exit_fails_pending() -> None:
    peer, proc = await _open_echo_peer()
    try:
        await peer.request("exit", {})
        # Peer exits; a subsequent request should eventually fail.
        with pytest.raises((TransportError, ProtocolError, TimeoutError)):
            await peer.request("after-exit", {}, timeout=2.0)
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)  # type: ignore[attr-defined]


async def test_unknown_inbound_method_returns_error() -> None:
    """Client-side: peer asks for a method we don't handle → error response.

    The echo peer's ping-client path is the reverse-request path; without a
    handler it should get Method not found, which surfaces as peer/ping-result
    with an error.
    """
    peer, proc = await _open_echo_peer()
    received: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    async def on_note(method: str, params: object) -> None:
        await received.put((method, params))

    peer.on_notification(on_note)
    # Deliberately do NOT register client/ping.
    try:
        await peer.request("ping-client", {})
        method, params = await asyncio.wait_for(received.get(), timeout=2.0)
        assert method == "peer/ping-result"
        assert isinstance(params, dict)
        assert params.get("result") is None
        assert params.get("error") is not None
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Regression: an inbound request handler that blocks (an approval parked on a
# user decision) must not stall the read loop. Everything else — outbound
# responses, notifications, further requests — has to keep flowing.
# ---------------------------------------------------------------------------

_RESPONDER = r'''
import sys, json
# Open an inbound request the client will park on, then serve whatever it asks.
sys.stdout.write(json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "needApproval", "params": {}}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if "method" in msg and msg.get("id") is not None:
        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "id": msg["id"], "result": {"served": msg["method"]}}) + "\n")
        sys.stdout.flush()
'''


async def test_blocked_handler_does_not_stall_outbound_requests() -> None:
    proc = await spawn_process([sys.executable, "-c", _RESPONDER])
    assert proc.stdout is not None and proc.stdin is not None
    peer = JsonRpcPeer(reader=proc.stdout, writer=proc.stdin)

    gate: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    entered = asyncio.Event()

    async def approval(method: str, params: object) -> object:
        entered.set()
        return {"decision": await gate}

    peer.on_request("needApproval", approval)
    peer.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)

        # The approval is still open. An interrupt must still round-trip.
        result = await asyncio.wait_for(peer.request("turn/interrupt", {}), timeout=5.0)
        assert result == {"served": "turn/interrupt"}

        gate.set_result("accept")
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)


async def test_concurrent_inbound_handlers_are_not_serialized() -> None:
    """Two open approvals should overlap, not queue behind one another."""
    child = r'''
import sys, json
for rid in (1, 2):
    sys.stdout.write(json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": "needApproval", "params": {"n": rid}}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    pass
'''
    proc = await spawn_process([sys.executable, "-c", child])
    assert proc.stdout is not None and proc.stdin is not None
    peer = JsonRpcPeer(reader=proc.stdout, writer=proc.stdin)

    open_calls = asyncio.Semaphore(0)
    release: asyncio.Event = asyncio.Event()

    async def approval(method: str, params: object) -> object:
        open_calls.release()
        await release.wait()
        return {"decision": "accept"}

    peer.on_request("needApproval", approval)
    peer.start()
    try:
        # Both handlers must be in flight at the same time.
        await asyncio.wait_for(open_calls.acquire(), timeout=5.0)
        await asyncio.wait_for(open_calls.acquire(), timeout=5.0)
        release.set()
    finally:
        await peer.aclose()
        await proc.aclose(timeout=2.0)


async def test_aclose_cancels_parked_handlers() -> None:
    proc = await spawn_process([sys.executable, "-c", _RESPONDER])
    assert proc.stdout is not None and proc.stdin is not None
    peer = JsonRpcPeer(reader=proc.stdout, writer=proc.stdin)

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def approval(method: str, params: object) -> object:
        entered.set()
        try:
            await asyncio.Event().wait()  # never resolves
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return None

    peer.on_request("needApproval", approval)
    peer.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        # aclose must not hang on the parked handler.
        await asyncio.wait_for(peer.aclose(), timeout=5.0)
        assert cancelled.is_set()
    finally:
        await proc.aclose(timeout=2.0)
