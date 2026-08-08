"""Cursor HarnessAdapter — ACP stdio over a RuntimeManager-bound process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import HarnessEvent, InteractionRequestedPayload
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
)
from talktoharnesses.providers.acp.connection import AcpConnection
from talktoharnesses.providers.acp.jsonrpc import JsonRpcRemoteError, ProtocolCloseError
from talktoharnesses.providers.acp.protocol import cursor_acp_protocol
from talktoharnesses.providers.acp.schemas.base import ALLOWED_OUTBOUND_METHODS
from talktoharnesses.providers.acp.schemas.cursor_ext import CURSOR_CONTROL_NOTIFICATIONS
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.cursor.argv import build_cursor_argv
from talktoharnesses.providers.cursor.compatibility import CursorReleaseRecord
from talktoharnesses.providers.cursor.normalizer import CursorNormalizer
from talktoharnesses.providers.cursor.probe import probe_cursor
from talktoharnesses.runtime.handle import ProcessHandle

logger = logging.getLogger(__name__)

CLIENT_INFO = {"name": "talktoharnesses", "version": "2026.8.0.dev7"}


def _map_dict(value: object) -> dict[str, Any]:
    # Accept partially-unknown JSON dicts under strict Pyright.
    if not isinstance(value, dict):
        return {}
    raw = cast(dict[object, object], cast(object, value))
    return {str(k): v for k, v in raw.items()}


class CursorAdapter:
    """Process-bound Cursor adapter. One instance per conversation runtime."""

    kind: HarnessKind = HarnessKind.CURSOR

    def __init__(self) -> None:
        self._process: ProcessHandle | None = None
        self._connection: AcpConnection | None = None
        self._normalizer = CursorNormalizer()
        self._release: CursorReleaseRecord | None = None
        self._capabilities: HarnessCapabilities | None = None
        self._session: HarnessSession | None = None
        self._event_q: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._prompt_task: asyncio.Task[None] | None = None
        self._pending_interactions: dict[UUID, tuple[str | int, list[dict[str, Any]]]] = {}
        self._closed = False
        self._active_prompt_request_id: int | None = None

    def bind_process(self, process: ProcessHandle) -> None:
        self._process = process

    def set_redaction_patterns(self, patterns: tuple[str, ...]) -> None:
        self._normalizer.set_redaction_patterns(patterns)

    def import_seen(
        self,
        native_ids: frozenset[str],
        stream_offsets: frozenset[str],
    ) -> None:
        self._normalizer.import_seen(native_ids, stream_offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return self._normalizer.export_seen()

    def build_argv(self, config: HarnessConfiguration) -> tuple[str, ...]:
        return build_cursor_argv(model=config.model, mode=config.mode)

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        build_cursor_argv(model=config.model, mode=config.mode)
        caps, release = await probe_cursor(config)
        self._capabilities = caps
        self._release = release
        return caps

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        await self._ensure_connection()
        assert self._connection is not None
        await self._initialize()
        cwd = request.launch.working_directory or request.configuration.working_directory
        future, _delivered = await self._connection.request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
        )
        result = await future
        session_id = _require_session_id(result)
        self._normalizer.set_session(session_id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CURSOR,
            native_session_id=session_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
        )
        self._session = session
        return session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        await self._ensure_connection()
        assert self._connection is not None
        await self._initialize()
        cwd = request.launch.working_directory or request.configuration.working_directory
        self._normalizer.set_session(request.native_session_id, resync=True)
        future, _delivered = await self._connection.request(
            "session/load",
            {
                "sessionId": request.native_session_id,
                "cwd": cwd,
                "mcpServers": [],
            },
        )
        result = await future
        session_id = _require_session_id(result) or request.native_session_id
        self._normalizer.set_session(session_id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CURSOR,
            native_session_id=session_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
        )
        self._session = session
        return session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self._require_session(session)
        assert self._connection is not None
        if not session.native_session_id:
            raise DomainError(ErrorCode.INVALID_STATE, "session has no native_session_id")
        self._normalizer.begin_turn(request.turn_id)
        future, _delivered = await self._connection.request(
            "session/prompt",
            {
                "sessionId": session.native_session_id,
                "prompt": [{"type": "text", "text": request.prompt}],
            },
        )
        # Return after frame drain (request() already drained). Watch response.
        self._prompt_task = asyncio.create_task(
            self._watch_prompt(future),
            name=f"cursor-prompt-{request.turn_id}",
        )

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        self._require_session(session)
        # Interject extension not proven for 1.0.0 fixtures yet.
        if self._release is None or not self._release.capabilities.supports_steer:
            return False
        return False

    async def interrupt(self, session: HarnessSession) -> None:
        self._require_session(session)
        assert self._connection is not None
        # Cancel pending permission waiters as cancelled outcomes.
        for interaction_id, (rpc_id, _options) in list(self._pending_interactions.items()):
            with contextlib.suppress(Exception):
                await self._connection.respond(
                    rpc_id,
                    {"outcome": {"outcome": "cancelled"}},
                )
            del self._pending_interactions[interaction_id]
        if session.native_session_id:
            await self._connection.notify(
                "session/cancel",
                {"sessionId": session.native_session_id},
            )

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        self._require_session(session)
        assert self._connection is not None
        pending = self._pending_interactions.get(answer.interaction_id)
        if pending is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "no pending interaction for answer",
                details={"interaction_id": str(answer.interaction_id)},
            )
        rpc_id, options = pending
        result = self._normalizer.map_approval_decision(answer.decision, options)
        outcome = result.get("outcome")
        outcome_map = _map_dict(outcome)
        # Reject unmapped decisions before popping the native waiter.
        if (
            answer.decision is not None
            and outcome_map.get("outcome") == "cancelled"
            and answer.decision.value != "cancel"
        ):
            raise DomainError(
                ErrorCode.INVALID_STATE,
                f"decision {answer.decision.value} not available on native request",
                details={"interaction_id": str(answer.interaction_id)},
            )
        del self._pending_interactions[answer.interaction_id]
        await self._connection.respond(rpc_id, result)

    def events(
        self,
        session: HarnessSession,
    ) -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
        self._require_session(session)

        async def _gen() -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
            while True:
                item = await self._event_q.get()
                if item is None:
                    return
                yield item

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        if self._closed:
            return
        self._closed = True
        if (
            self._prompt_task is not None
            and self._prompt_task is not asyncio.current_task()
            and not self._prompt_task.done()
        ):
            self._prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._prompt_task
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        with contextlib.suppress(asyncio.QueueFull):
            self._event_q.put_nowait(None)

    async def _ensure_connection(self) -> None:
        if self._process is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "CursorAdapter has no bound process; RuntimeManager must bind_process first",
            )
        if self._connection is None:
            conn = AcpConnection(self._process, protocol=cursor_acp_protocol())
            conn.set_notification_handler("session/update", self._on_session_update)
            for method in CURSOR_CONTROL_NOTIFICATIONS:
                conn.set_notification_handler(method, self._on_control_notification)
            conn.set_request_handler(
                "session/request_permission",
                self._on_permission_request,
            )
            await conn.start()
            self._connection = conn

    async def _initialize(self) -> None:
        assert self._connection is not None
        future, _ = await self._connection.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": CLIENT_INFO,
                # No client fs/terminal capabilities unless fixtures prove reverse handlers.
                "clientCapabilities": {},
            },
        )
        result = await future
        if not isinstance(result, dict):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "initialize result must be an object")
        result_map = _map_dict(cast(object, result))
        protocol = result_map.get("protocolVersion")
        if protocol != 1:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "ACP protocol version mismatch",
                details={"protocolVersion": protocol},
            )
        if self._release is not None and protocol != self._release.acp_protocol_version:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "ACP protocol version does not match compatibility record",
                details={
                    "protocolVersion": protocol,
                    "expected": self._release.acp_protocol_version,
                },
            )
        if self._release is not None:
            self._validate_initialize_identity(result_map)

    def _validate_initialize_identity(self, result: dict[str, Any]) -> None:
        assert self._release is not None
        agent_info = _map_dict(result.get("agentInfo"))
        if (
            agent_info.get("name") != self._release.agent_name
            or agent_info.get("version") != self._release.cli_version
        ):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "initialize agent identity does not match compatibility record",
                details={"agentInfo": agent_info, "release_id": self._release.id},
            )
        capabilities = _map_dict(result.get("agentCapabilities"))
        if (
            self._release.capabilities.supports_resume
            and capabilities.get("loadSession") is not True
        ):
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "initialize result does not advertise session loading",
                details={"release_id": self._release.id},
            )
        missing_methods = set(self._release.required_agent_methods) - ALLOWED_OUTBOUND_METHODS
        if missing_methods:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "adapter does not implement required agent methods",
                details={"missing_methods": sorted(missing_methods)},
            )

    async def _watch_prompt(self, future: asyncio.Future[Any]) -> None:
        try:
            result = await future
        except asyncio.CancelledError:
            return
        except ProtocolCloseError as exc:
            await self._emit_prompt_outcome_unknown_and_close(exc.message)
            return
        except JsonRpcRemoteError as exc:
            events = self._normalizer.on_prompt_terminal(
                "error",
                error_message=exc.message,
            )
            await self._emit_many(events)
            return
        except DomainError as exc:
            await self._emit_prompt_outcome_unknown_and_close(exc.message)
            return
        except Exception as exc:
            events = self._normalizer.on_prompt_terminal("error", error_message=str(exc))
            await self._emit_many(events)
            return

        stop_reason = "end_turn"
        if isinstance(result, dict):
            result_map = _map_dict(cast(object, result))
            raw = result_map.get("stopReason") or result_map.get("stop_reason")
            if isinstance(raw, str):
                stop_reason = raw
        events = self._normalizer.on_prompt_terminal(stop_reason)
        await self._emit_many(events)

    async def _emit_prompt_outcome_unknown_and_close(self, message: str) -> None:
        # These errors come from an ACP connection that has already stopped
        # accepting writes. Persist the uncertain outcome before closing only
        # this adapter's event stream.
        events = self._normalizer.on_prompt_outcome_unknown(message)
        await self._emit_many(events)
        await self._event_q.put(None)

    async def _on_session_update(self, notification: Any) -> None:
        params = _map_dict(cast(object, notification.params))
        events = self._normalizer.on_session_update(params)
        await self._emit_many(events)

    async def _on_control_notification(self, notification: Any) -> None:
        # Strictly decoded at the connection layer; intentionally ignored for transcript.
        logger.debug("ignoring Cursor control notification %s", notification.method)

    async def _on_permission_request(self, request: Any) -> Any | None:
        params = _map_dict(request.params)
        interaction_id = uuid4()
        options_obj = params.get("options")
        options: list[dict[str, Any]] = []
        if isinstance(options_obj, list):
            for item in cast(list[object], options_obj):
                mapped = _map_dict(item)
                if mapped:
                    options.append(mapped)
        self._pending_interactions[interaction_id] = (request.id, options)
        events = self._normalizer.on_permission_request(
            params,
            interaction_id=interaction_id,
        )
        correlation = {"json_rpc_request_id": str(request.id)}
        tool_call = _map_dict(params.get("toolCall"))
        tool_call_id = tool_call.get("toolCallId")
        if isinstance(tool_call_id, str):
            correlation["tool_call_id"] = tool_call_id
        session_id = params.get("sessionId")
        if isinstance(session_id, str):
            correlation["native_session_id"] = session_id
        for event in events:
            if isinstance(event, InteractionRequestedPayload):
                await self._event_q.put(
                    HarnessInteractionRequest(
                        payload=event,
                        provider_correlation=correlation,
                    )
                )
            else:
                await self._event_q.put(event)
        # Respond later via answer_interaction.
        return None

    async def _emit_many(self, events: list[HarnessEvent]) -> None:
        for event in events:
            await self._event_q.put(event)

    def _require_session(self, session: HarnessSession) -> None:
        if self._session is None:
            raise DomainError(ErrorCode.INVALID_STATE, "adapter has no active session")
        if self._closed:
            raise DomainError(ErrorCode.INVALID_STATE, "adapter is closed")


def _require_session_id(result: Any) -> str:
    if not isinstance(result, dict):
        raise DomainError(ErrorCode.PROTOCOL_ERROR, "session result must be an object")
    result_map = _map_dict(cast(object, result))
    session_id = result_map.get("sessionId") or result_map.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise DomainError(ErrorCode.PROTOCOL_ERROR, "session result missing sessionId")
    return session_id
