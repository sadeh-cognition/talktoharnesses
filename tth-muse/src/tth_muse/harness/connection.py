"""MSP v1 JSON-RPC over the supervised Muse host's stdio."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID, uuid4

from tth_types.enums import ErrorCode
from tth_types.errors import DomainError

from tth_muse.harness.framing import iter_json_frames
from tth_muse.harness.wire_log import WireLog
from tth_muse.runtime.handle import ProcessHandle

logger = logging.getLogger(__name__)


def _frame_params(frame: dict[str, Any]) -> dict[str, Any]:
    params = frame.get("params")
    return cast(dict[str, Any], params) if isinstance(params, dict) else {}


def _trace(frame: dict[str, Any]) -> dict[str, Any]:
    params = _frame_params(frame)
    return {
        "msp_turn_id": params.get("turnId"),
        "msp_session_id": params.get("sessionId"),
    }


class MuseConnection:
    def __init__(
        self,
        process: ProcessHandle,
        notification: Callable[[str, dict[str, Any]], Awaitable[None]],
        disconnected: Callable[[str], Awaitable[None]],
        redact: Callable[[str], str] = str,
        wire: WireLog | None = None,
    ) -> None:
        self.process = process
        self._notification = notification
        self._disconnected = disconnected
        self._redact = redact
        self._wire = wire
        self.frames_in = 0
        self.frames_out = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._command_ms = -1
        self._command_sequence = 0
        self._router = asyncio.create_task(self._read())

    def mint_command_id(self) -> str:
        """Match the official SDK's per-connection monotonic UUIDv7 minter."""
        now = time.time_ns() // 1_000_000
        if now <= self._command_ms:
            now = self._command_ms
            self._command_sequence += 1
            if self._command_sequence > 0xFFF:
                now += 1
                self._command_sequence = 0
        else:
            self._command_sequence = 0
        self._command_ms = now
        return str(
            UUID(
                int=(now << 80)
                | (7 << 76)
                | (self._command_sequence << 64)
                | (2 << 62)
                | secrets.randbits(62)
            )
        )

    async def request(self, method: str, **params: Any) -> dict[str, Any]:
        if self._closed:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "Muse host connection is closed")
        identity = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}
            )
            return await future
        finally:
            self._pending.pop(identity, None)

    async def command(self, method: str, **params: Any) -> dict[str, Any]:
        if "commandId" not in params:
            params["commandId"] = self.mint_command_id()
        for attempt in range(3):
            try:
                result = await self.request(method, **params)
            except DomainError as error:
                # SDK retries only these explicit non-admission errors, using
                # the same command identity and payload. Internal errors may
                # have applied the decision and must not be replayed here.
                retryable = (
                    error.details.get("native_error_code"),
                    error.details.get("native_error_kind"),
                ) in {(-32001, "overloaded"), (-32031, "backpressured")}
                if not retryable or attempt == 2:
                    raise
                await asyncio.sleep(secrets.randbelow(50 * 2**attempt + 1) / 1000)
                continue
            # session/start and session/resume omit the echo in MSP v1.
            if "commandId" in result and result["commandId"] != params["commandId"]:
                raise DomainError(
                    ErrorCode.PROTOCOL_ERROR, "Muse acknowledgment commandId mismatch"
                )
            return result
        raise AssertionError("unreachable command retry state")

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize", clientInfo={"name": "talktoharnesses", "version": "0.1.0"}
        )
        if result.get("schema", {}).get("version") != 1:
            raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "Muse host does not speak MSP v1")
        if result.get("sessionDurability", "durable") != "durable":
            raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "Muse host must persist sessions")
        await self._write({"jsonrpc": "2.0", "method": "initialized"})
        return result

    async def _write(self, frame: dict[str, Any]) -> None:
        async with self._lock:
            self.frames_out += 1
            if self._wire is not None:
                self._wire.outbound(frame)
            logger.debug(
                "msp -> method=%s id=%s", frame.get("method"), frame.get("id"), extra=_trace(frame)
            )
            await self.process.write_stdin((json.dumps(frame) + "\n").encode())

    async def _read(self) -> None:
        message = "Muse host stdout closed"
        logger.info("msp reader started")
        if self._wire is not None:
            self._wire.note("reader started")
        try:
            async for frame in iter_json_frames(self.process.stdout()):
                self.frames_in += 1
                if self._wire is not None:
                    self._wire.inbound(frame)
                params = _frame_params(frame)
                logger.debug(
                    "msp <- method=%s id=%s turnId=%s sessionId=%s",
                    frame.get("method"),
                    frame.get("id"),
                    params.get("turnId"),
                    params.get("sessionId"),
                )
                if frame.get("jsonrpc") != "2.0":
                    raise DomainError(ErrorCode.PROTOCOL_ERROR, "Invalid MSP envelope")
                if "method" in frame:
                    # MSP evolves additively: the adapter ignores unfamiliar notifications.
                    if "id" in frame:
                        if frame["method"] in {"approval/request", "userInput/request"}:
                            # Acknowledge receipt; decisions use a separate command.
                            await self._write({"jsonrpc": "2.0", "id": frame["id"], "result": {}})
                            if frame["method"] == "userInput/request":
                                # Meta's user-input transcript carries the ask
                                # only as a request, unlike approval/requested.
                                await self._dispatch_notification(
                                    "userInput/requested", frame.get("params")
                                )
                        else:
                            await self._write(
                                {
                                    "jsonrpc": "2.0",
                                    "id": frame["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Unsupported client method",
                                    },
                                }
                            )
                    else:
                        await self._dispatch_notification(frame["method"], frame.get("params"))
                else:
                    future = self._pending.get(str(frame.get("id")))
                    if future is None or future.done():
                        continue
                    if "error" in frame:
                        error = frame["error"]
                        error_data: dict[str, Any] = error.get("data") or {}
                        future.set_exception(
                            DomainError(
                                ErrorCode.PROTOCOL_ERROR,
                                self._redact(
                                    f"Muse MSP error {error.get('code')}: "
                                    f"{error.get('message', 'request rejected')}"
                                ),
                                details={
                                    "native_error_code": error.get("code"),
                                    "native_error_kind": error_data.get("kind"),
                                    "native_error_reason": self._redact(
                                        str(error_data.get("reason", ""))
                                    ),
                                },
                            )
                        )
                    elif isinstance(frame.get("result"), dict):
                        future.set_result(frame["result"])
                    else:
                        raise DomainError(ErrorCode.PROTOCOL_ERROR, "Invalid MSP result")
        except asyncio.CancelledError:
            message = "Muse host reader cancelled"
            raise
        except Exception:
            message = "Muse host protocol stream failed"
            # The traceback lives here; the frame counts are on the
            # lifecycle summary in ``finally``.
            logger.exception("msp reader failed")
        finally:
            logger.info(
                "msp reader exiting: %s (frames in=%d out=%d pending=%d)",
                message,
                self.frames_in,
                self.frames_out,
                sum(1 for f in self._pending.values() if not f.done()),
            )
            if self._wire is not None:
                self._wire.note(
                    "reader exiting",
                    reason=message,
                    frames_in=self.frames_in,
                    frames_out=self.frames_out,
                )
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(DomainError(ErrorCode.PROTOCOL_ERROR, message))
            await self._disconnected(message)

    async def _dispatch_notification(self, method: str, params: object) -> None:
        # One malformed or unexpected notification must not tear down the
        # whole connection (which fails every pending request as unknown).
        try:
            await self._notification(
                method, cast(dict[str, Any], params) if isinstance(params, dict) else {}
            )
        except Exception:
            logger.exception("Muse notification handler failed method=%s", method)
            if self._wire is not None:
                self._wire.note("notification handler failed", method=method)

    async def close(self) -> None:
        self._closed = True
        await self.process.close_stdin()
        self._router.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._router
