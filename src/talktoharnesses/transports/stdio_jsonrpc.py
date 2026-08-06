"""Bidirectional newline-delimited JSON-RPC 2.0 peer over asyncio streams.

- Outbound requests resolve via ``asyncio.Future`` keyed by id.
- Inbound requests are dispatched to registered handlers.
- Notifications fan out to registered subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from talktoharnesses.errors import ProtocolError, TransportError

logger = logging.getLogger(__name__)

JsonDict = dict[str, Any]
RequestHandler = Callable[[str, Any], Awaitable[Any]]
NotificationHandler = Callable[[str, Any], Awaitable[None] | None]


@dataclass
class JsonRpcPeer:
    """JSON-RPC 2.0 peer bound to a reader/writer pair (typically process stdio)."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    _next_id: int = field(default=1, init=False, repr=False)
    _pending: dict[int | str, asyncio.Future[Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _request_handlers: dict[str, RequestHandler] = field(
        default_factory=dict, init=False, repr=False
    )
    _notification_handlers: list[NotificationHandler] = field(
        default_factory=list, init=False, repr=False
    )
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _handler_tasks: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False
    )
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def on_request(self, method: str, handler: RequestHandler) -> None:
        """Register a handler for inbound requests with the given method name."""
        self._request_handlers[method] = handler

    def on_notification(self, handler: NotificationHandler) -> None:
        """Register a fan-out subscriber for all inbound notifications."""
        self._notification_handlers.append(handler)

    def start(self) -> None:
        """Start the background reader loop (idempotent)."""
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="jsonrpc-reader"
            )

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | list[Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send a request and await the matching response."""
        if self._closed:
            raise TransportError("JSON-RPC peer is closed")
        self.start()

        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut

        message: JsonDict = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        try:
            await self._send(message)
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        except BaseException:
            # Covers send failures, timeouts, and cancellation alike: the
            # request is no longer outstanding, so drop it from the table.
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | list[Any] | None = None,
    ) -> None:
        """Send a notification (no response expected)."""
        if self._closed:
            raise TransportError("JSON-RPC peer is closed")
        message: JsonDict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def respond(self, req_id: int | str, result: Any) -> None:
        """Send a successful response to an inbound request."""
        await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def respond_error(
        self,
        req_id: int | str | None,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        """Send an error response."""
        error: JsonDict = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        payload: JsonDict = {"jsonrpc": "2.0", "error": error}
        if req_id is not None:
            payload["id"] = req_id
        await self._send(payload)

    async def aclose(self) -> None:
        """Stop the reader and fail any pending requests."""
        if self._closed:
            return
        self._closed = True

        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — best-effort shutdown
                logger.debug("reader task error during aclose", exc_info=True)

        # Inbound handlers may still be parked on a user decision.
        handlers = list(self._handler_tasks)
        for task in handlers:
            if not task.done():
                task.cancel()
        for task in handlers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._handler_tasks.clear()

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(TransportError("JSON-RPC peer closed"))
        self._pending.clear()

        if not self.writer.is_closing():
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def _send(self, message: JsonDict) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._lock:
            if self._closed:
                raise TransportError("JSON-RPC peer is closed")
            try:
                self.writer.write(data)
                await self.writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise TransportError(f"failed to write JSON-RPC message: {exc}") from exc

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                line = await self.reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("invalid JSON from peer: %s", exc)
                    continue
                if not isinstance(message, dict):
                    logger.warning("non-object JSON-RPC message ignored")
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("JSON-RPC read loop ended: %s", exc)
            self._fail_pending(TransportError(f"JSON-RPC read loop failed: {exc}"))
        finally:
            # Peer EOF — fail outstanding requests.
            if self._pending:
                self._fail_pending(TransportError("JSON-RPC peer closed connection"))

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _dispatch(self, message: JsonDict) -> None:
        if "method" in message:
            # Request or notification
            if "id" in message and message["id"] is not None:
                # Handlers may block indefinitely (an approval waits on the
                # user). Run them off the read loop so the peer keeps making
                # progress: further notifications, and — critically — responses
                # to our own outbound requests such as ``turn/interrupt``.
                task = asyncio.create_task(
                    self._handle_inbound_request(message),
                    name=f"jsonrpc-handler-{message.get('method')}",
                )
                self._handler_tasks.add(task)
                task.add_done_callback(self._handler_tasks.discard)
            else:
                await self._handle_notification(message)
            return

        # Response
        if "id" not in message:
            raise ProtocolError("JSON-RPC response missing id")
        req_id = message["id"]
        fut = self._pending.pop(req_id, None)
        if fut is None or fut.done():
            logger.debug("unexpected response id=%s", req_id)
            return
        if "error" in message and message["error"] is not None:
            err = message["error"]
            if isinstance(err, dict):
                fut.set_exception(
                    ProtocolError(
                        f"JSON-RPC error {err.get('code')}: {err.get('message')}"
                    )
                )
            else:
                fut.set_exception(ProtocolError(f"JSON-RPC error: {err!r}"))
            return
        fut.set_result(message.get("result"))

    async def _handle_inbound_request(self, message: JsonDict) -> None:
        method = message.get("method")
        req_id = message.get("id")
        response_id: int | str | None = req_id if isinstance(req_id, (int, str)) else None
        if not isinstance(method, str):
            await self.respond_error(response_id, -32600, "Invalid Request")
            return
        handler = self._request_handlers.get(method)
        if handler is None:
            await self.respond_error(response_id, -32601, f"Method not found: {method}")
            return
        params = message.get("params")
        try:
            result = await handler(method, params)
        except Exception as exc:  # noqa: BLE001 — map to JSON-RPC error
            logger.exception("handler for %s failed", method)
            await self._respond_best_effort(
                self.respond_error(response_id, -32000, str(exc)), method
            )
            return
        if response_id is None:
            return
        await self._respond_best_effort(self.respond(response_id, result), method)

    async def _respond_best_effort(self, send: Awaitable[None], method: str) -> None:
        """Send a response, tolerating a peer that closed while we were working.

        Handlers now run off the read loop and may outlive ``aclose()`` (an
        approval parked on a user decision, for instance). Losing the response
        at that point is expected, not an error worth propagating into an
        orphaned task.
        """
        try:
            await send
        except TransportError:
            logger.debug("peer closed before response to %s could be sent", method)

    async def _handle_notification(self, message: JsonDict) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        for handler in list(self._notification_handlers):
            try:
                result = handler(method, params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 — isolate fan-out failures
                logger.exception("notification handler failed for %s", method)
