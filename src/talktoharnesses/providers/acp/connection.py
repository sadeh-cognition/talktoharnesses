"""Minimal ACP JSON-RPC connection over a supervised ProcessHandle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers.acp.framing import FrameDecodeError, iter_json_frames
from talktoharnesses.providers.acp.jsonrpc import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRemoteError,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    ProtocolCloseError,
    encode_frame,
    parse_envelope,
)
from talktoharnesses.providers.acp.schemas.base import (
    GROK_CONTROL_NOTIFICATIONS,
    is_allowlisted_method,
    is_allowlisted_session_update,
)
from talktoharnesses.runtime.handle import ProcessHandle

logger = logging.getLogger(__name__)

RequestHandler = Callable[[JsonRpcRequest], Awaitable[Any | None]]
NotificationHandler = Callable[[JsonRpcNotification], Awaitable[None]]


class Delivered:
    """Marker that an outbound request frame has been flushed to stdin."""

    __slots__ = ()


class AcpConnection:
    """Single-consumer NDJSON JSON-RPC over ProcessHandle stdout/stdin."""

    def __init__(self, process: ProcessHandle) -> None:
        self._process = process
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[str | int, asyncio.Future[Any]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._notification_handlers: dict[str, NotificationHandler] = {}
        self._router_task: asyncio.Task[None] | None = None
        self._closed = False
        self._accepting_writes = True
        self._started = False

    def set_request_handler(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    def set_notification_handler(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers[method] = handler

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "cannot start a closed ACP connection")
        self._started = True
        self._router_task = asyncio.create_task(self._route_stdout(), name="acp-router")

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        allow_unknown_method: bool = False,
    ) -> tuple[asyncio.Future[Any], Delivered]:
        """Send a request; return (response future, delivered marker after drain)."""
        if not allow_unknown_method and not is_allowlisted_method(method, direction="outbound"):
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"outbound method not allowlisted: {method}",
                details={"method": method},
            )
        if self._closed or not self._accepting_writes:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "ACP connection is closed")

        request_id = self._allocate_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
        }
        if params is not None:
            payload["params"] = params
        try:
            await self._write_frame(payload)
        except Exception:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise
        return future, Delivered()

    async def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        allow_unknown_method: bool = False,
    ) -> None:
        if not allow_unknown_method and not is_allowlisted_method(method, direction="outbound"):
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"outbound method not allowlisted: {method}",
                details={"method": method},
            )
        if self._closed or not self._accepting_writes:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "ACP connection is closed")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_frame(payload)

    async def respond(self, request_id: str | int, result: Any) -> None:
        await self._write_frame(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        )

    async def respond_error(
        self,
        request_id: str | int,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._write_frame({"jsonrpc": "2.0", "id": request_id, "error": error})

    def cancel_pending(self, request_id: str | int) -> bool:
        """Cancel a local waiter. Returns True if this call won the race."""
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.cancel()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting_writes = False
        task = self._router_task
        self._router_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._fail_pending(ProtocolCloseError("ACP connection closed"))

    def _allocate_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        if request_id in self._pending:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "duplicate live JSON-RPC request id",
                details={"id": request_id},
            )
        return request_id

    async def _write_frame(self, payload: dict[str, Any]) -> None:
        async with self._write_lock:
            if self._closed or not self._accepting_writes:
                raise DomainError(ErrorCode.PROTOCOL_ERROR, "ACP connection is closed")
            await self._process.write_stdin(encode_frame(payload))

    async def _route_stdout(self) -> None:
        try:
            async for obj in iter_json_frames(self._process.stdout()):
                try:
                    await self._dispatch(obj)
                except DomainError as exc:
                    logger.warning("ACP protocol fault: %s", exc)
                    self._accepting_writes = False
                    self._fail_pending(exc)
                    return
        except FrameDecodeError as exc:
            err = DomainError(
                ErrorCode.PROTOCOL_ERROR,
                exc.message,
            )
            self._accepting_writes = False
            self._fail_pending(err)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = DomainError(
                ErrorCode.PROTOCOL_ERROR,
                f"ACP router failed: {exc}",
            )
            self._accepting_writes = False
            self._fail_pending(err)
        else:
            # EOF: clean end of stream.
            self._accepting_writes = False
            self._fail_pending(ProtocolCloseError("ACP stdout EOF"))

    async def _dispatch(self, obj: dict[str, Any]) -> None:
        try:
            envelope = parse_envelope(obj)
        except Exception as exc:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                f"invalid JSON-RPC envelope: {exc}",
            ) from exc

        if isinstance(envelope, JsonRpcSuccessResponse):
            self._resolve_response(envelope.id, envelope.result, error=None)
            return
        if isinstance(envelope, JsonRpcErrorResponse):
            if envelope.id is None:
                raise DomainError(ErrorCode.PROTOCOL_ERROR, "error response missing id")
            remote = JsonRpcRemoteError(
                envelope.error.code,
                envelope.error.message,
                envelope.error.data,
            )
            self._resolve_response(envelope.id, None, error=remote)
            return
        if isinstance(envelope, JsonRpcRequest):
            await self._handle_inbound_request(envelope)
            return
        # Remaining arm is notification (parse_envelope only returns these four).
        await self._handle_inbound_notification(envelope)

    def _resolve_response(
        self,
        request_id: str | int,
        result: Any,
        *,
        error: BaseException | None,
    ) -> None:
        future = self._pending.pop(request_id, None)
        if future is None:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "response for unknown request id",
                details={"id": str(request_id)},
            )
        if future.done():
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "duplicate response for request id",
                details={"id": str(request_id)},
            )
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    async def _handle_inbound_request(self, request: JsonRpcRequest) -> None:
        if not is_allowlisted_method(request.method, direction="inbound"):
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"inbound request method not allowlisted: {request.method}",
                details={"method": request.method},
            )
        handler = self._request_handlers.get(request.method)
        if handler is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"no handler for inbound request: {request.method}",
                details={"method": request.method},
            )
        # Handlers may respond later (permission); do not await completion of
        # the peer wait here if the handler returns None without writing.
        result = await handler(request)
        if result is not None:
            await self.respond(request.id, result)

    async def _handle_inbound_notification(self, notification: JsonRpcNotification) -> None:
        method = notification.method
        if method in GROK_CONTROL_NOTIFICATIONS:
            handler = self._notification_handlers.get(method)
            if handler is not None:
                await handler(notification)
            # Control-plane: strictly accepted, optionally handled, ignored by default.
            return

        if method == "session/update":
            params = notification.params if isinstance(notification.params, dict) else None
            if not is_allowlisted_session_update(params):
                raise DomainError(
                    ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                    "session/update variant not allowlisted",
                    details={"params": notification.params},
                )
        elif not is_allowlisted_method(method, direction="inbound"):
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"inbound notification not allowlisted: {method}",
                details={"method": method},
            )

        handler = self._notification_handlers.get(method)
        if handler is None:
            # Allowlisted but unhandled: still an error for non-control methods
            # that the adapter must observe (session/update requires a handler).
            if method == "session/update":
                raise DomainError(
                    ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                    "no handler for session/update",
                )
            return
        await handler(notification)

    def _fail_pending(self, error: BaseException) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        for _request_id, future in pending:
            if not future.done():
                future.set_exception(error)
