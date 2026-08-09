"""OpenCode HarnessAdapter — process-bound serve + loopback HTTP/SSE."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import HarnessEvent, InteractionRequestedPayload
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
)
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.opencode.argv import build_opencode_argv
from talktoharnesses.providers.opencode.compatibility import (
    OpenCodeReleaseRecord,
    enforce_published_operation,
)
from talktoharnesses.providers.opencode.normalizer import OpenCodeNormalizer
from talktoharnesses.providers.opencode.probe import probe_opencode
from talktoharnesses.providers.opencode.schemas import OpenCodeHealth, OpenCodeSession
from talktoharnesses.providers.opencode.sse import SseDecoder
from talktoharnesses.runtime.handle import ProcessHandle

logger = logging.getLogger(__name__)

HttpClientFactory = Callable[[str], Any]


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class OpenCodeAdapter:
    """Process-bound OpenCode adapter. One instance per conversation runtime."""

    kind: HarnessKind = HarnessKind.OPENCODE

    def __init__(self, *, http_client_factory: HttpClientFactory | None = None) -> None:
        self._http_client_factory = http_client_factory
        self._process: ProcessHandle | None = None
        self._port: int | None = None
        self._base_url: str | None = None
        self._client: Any | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._normalizer = OpenCodeNormalizer()
        self._release: OpenCodeReleaseRecord | None = None
        self._capabilities: HarnessCapabilities | None = None
        self._session: HarnessSession | None = None
        self._event_q: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._pending_interactions: dict[UUID, str] = {}
        self._closed = False
        self._connected_event = asyncio.Event()

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
        del config
        if self._port is None:
            self._port = _allocate_loopback_port()
        return build_opencode_argv(port=self._port)

    def prepare_port(self, port: int | None = None) -> int:
        """Test hook: set/allocate the serve port before build_argv/spawn."""
        self._port = port if port is not None else _allocate_loopback_port()
        return self._port

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        caps, release = await probe_opencode(config)
        self._capabilities = caps
        self._release = release
        return caps

    def preflight_operation(self, mode: Literal["create", "resume"]) -> None:
        if self._release is None:
            raise DomainError(
                ErrorCode.INVALID_STATE, "opencode adapter must be probed before operation"
            )
        enforce_published_operation(self._release, mode=mode)

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        self.preflight_operation("create")
        await self._ensure_http()
        await self._wait_healthy()
        await self._open_events()
        assert self._client is not None
        response = await self._client.post(
            "/session",
            json={"directory": request.launch.working_directory},
        )
        self._raise_http(response, "POST /session")
        session_body = OpenCodeSession.model_validate(response.json())
        self._normalizer.set_session(session_body.id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.OPENCODE,
            native_session_id=session_body.id,
            model=request.configuration.model,
            mode=request.configuration.mode,
        )
        self._session = session
        return session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        self.preflight_operation("resume")
        await self._ensure_http()
        await self._wait_healthy()
        await self._open_events()
        assert self._client is not None
        self._normalizer.set_session(request.native_session_id, resync=True)
        response = await self._client.get(f"/session/{request.native_session_id}")
        if response.status_code == 404:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "opencode session not found for resume",
                details={"native_session_id": request.native_session_id},
            )
        self._raise_http(response, "GET /session/{id}")
        session_body = OpenCodeSession.model_validate(response.json())
        if session_body.id != request.native_session_id:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "opencode resume session id mismatch",
            )
        self._normalizer.set_session(session_body.id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.OPENCODE,
            native_session_id=session_body.id,
            model=request.configuration.model,
            mode=request.configuration.mode,
        )
        self._session = session
        return session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self._require_session(session)
        assert self._client is not None
        if not session.native_session_id:
            raise DomainError(ErrorCode.INVALID_STATE, "session has no native_session_id")
        self._normalizer.begin_turn(request.turn_id)
        message_id = str(uuid4())
        payload = {
            "parts": [{"type": "text", "text": request.prompt}],
            "model": request.model or session.model,
            "agent": session.mode,
            "messageID": message_id,
        }
        response = await self._client.post(
            f"/session/{session.native_session_id}/prompt_async",
            json=payload,
        )
        self._raise_http(response, "POST /session/{id}/prompt_async")

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        self._require_session(session)
        del request
        return False

    async def interrupt(self, session: HarnessSession) -> None:
        self._require_session(session)
        assert self._client is not None
        for interaction_id in list(self._pending_interactions):
            permission_id = self._pending_interactions.pop(interaction_id)
            with contextlib.suppress(Exception):
                await self._client.post(
                    f"/session/{session.native_session_id}/permissions/{permission_id}",
                    json={"response": "reject", "remember": False},
                )
        if session.native_session_id:
            with contextlib.suppress(Exception):
                await self._client.post(f"/session/{session.native_session_id}/abort")

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        self._require_session(session)
        assert self._client is not None
        permission_id = self._pending_interactions.get(answer.interaction_id)
        if permission_id is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "no pending interaction for answer",
                details={"interaction_id": str(answer.interaction_id)},
            )
        decision = answer.decision
        if decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_SESSION}:
            response_value = "once"
        elif decision is ApprovalDecision.DENY:
            response_value = "reject"
        else:
            response_value = "reject"
        del self._pending_interactions[answer.interaction_id]
        response = await self._client.post(
            f"/session/{session.native_session_id}/permissions/{permission_id}",
            json={"response": response_value, "remember": False},
        )
        self._raise_http(response, "POST permissions")

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
        del session
        if self._closed:
            return
        self._closed = True
        await self._close_http()
        self._pending_interactions.clear()
        with contextlib.suppress(asyncio.QueueFull):
            self._event_q.put_nowait(None)

    async def retry_startup(self, error: DomainError) -> tuple[str, ...] | None:
        """Reset a failed pre-session server after an ephemeral-port bind race."""
        if (
            error.code is not ErrorCode.RUNTIME_TIMEOUT
            or self._session is not None
            or self._process is None
            or self._process.returncode is None
        ):
            return None
        await self._close_http()
        self._process = None
        self._base_url = None
        self._port = _allocate_loopback_port()
        self._connected_event.clear()
        return build_opencode_argv(port=self._port)

    async def _close_http(self) -> None:
        if (
            self._sse_task is not None
            and self._sse_task is not asyncio.current_task()
            and not self._sse_task.done()
        ):
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sse_task
        self._sse_task = None
        if self._client is not None:
            aclose = getattr(self._client, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    result = aclose()
                    if asyncio.iscoroutine(result):
                        await result
            self._client = None

    async def _ensure_http(self) -> None:
        if self._client is not None:
            return
        if self._port is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "OpenCodeAdapter has no port; build_argv/prepare_port must run before start",
            )
        self._base_url = f"http://127.0.0.1:{self._port}"
        if self._http_client_factory is not None:
            self._client = self._http_client_factory(self._base_url)
            return
        try:
            import httpx
        except ImportError as exc:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "httpx (opencode extra) is not installed",
                details={"extra": "opencode"},
            ) from exc
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=5.0, write=5.0, read=None, pool=5.0),
        )

    async def _wait_healthy(self) -> None:
        assert self._client is not None
        assert self._release is not None
        last_error: Exception | None = None
        for _ in range(50):
            if self._process is not None and self._process.returncode is not None:
                raise DomainError(
                    ErrorCode.RUNTIME_TIMEOUT,
                    "opencode process exited before session startup",
                    details={"returncode": self._process.returncode},
                )
            try:
                response = await self._client.get("/global/health")
                if response.status_code == 200:
                    health = OpenCodeHealth.model_validate(response.json())
                    if not health.healthy:
                        raise DomainError(
                            ErrorCode.PROVIDER_INCOMPATIBLE,
                            "opencode health reported unhealthy",
                        )
                    if health.version != self._release.cli_version:
                        raise DomainError(
                            ErrorCode.PROVIDER_INCOMPATIBLE,
                            "opencode health version mismatch",
                            details={
                                "health_version": health.version,
                                "expected": self._release.cli_version,
                            },
                        )
                    return
            except DomainError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            await asyncio.sleep(0.1)
        raise DomainError(
            ErrorCode.RUNTIME_TIMEOUT,
            "opencode health check timed out",
            details={"error": str(last_error) if last_error else None},
        )

    async def _open_events(self) -> None:
        if self._sse_task is not None and not self._sse_task.done():
            return
        self._connected_event.clear()
        self._sse_task = asyncio.create_task(self._sse_loop(), name="opencode-sse")
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=5.0)
        except TimeoutError as exc:
            raise DomainError(
                ErrorCode.RUNTIME_TIMEOUT,
                "opencode SSE connected event not received",
            ) from exc

    async def _sse_loop(self) -> None:
        assert self._client is not None
        decoder = SseDecoder()
        try:
            async with self._client.stream("GET", "/event") as response:
                self._raise_http(response, "GET /event")
                async for chunk in response.aiter_bytes():
                    for sse in decoder.feed(chunk):
                        await self._dispatch_sse(sse.event, sse.data)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("opencode SSE disconnected: %s", exc)
        if not self._closed:
            await self._reconnect_resync()

    async def _dispatch_sse(self, event_name: str | None, data: str) -> None:
        if not data:
            if event_name == "server.connected":
                self._connected_event.set()
            return
        try:
            raw_obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "invalid JSON in SSE data",
            ) from exc
        if not isinstance(raw_obj, dict):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "SSE data must be an object")
        raw = {str(k): v for k, v in cast(dict[object, object], raw_obj).items()}
        event_type = str(raw.get("type") or event_name or "")
        if event_type == "server.connected":
            self._connected_event.set()
            return
        if event_type == "permission.asked":
            props = raw.get("properties")
            props_map = (
                {str(k): v for k, v in cast(dict[object, object], props).items()}
                if isinstance(props, dict)
                else raw
            )
            session_id = props_map.get("sessionID") or props_map.get("session_id")
            if not isinstance(session_id, str):
                raise DomainError(
                    ErrorCode.PROTOCOL_ERROR,
                    "permission event missing session id",
                )
            if not self._normalizer.accepts_session(session_id):
                return
            await self._handle_permission(props_map)
            return
        # Normalize envelope: either flat or {type, properties}
        if "properties" not in raw and event_type:
            envelope = {
                "type": event_type,
                "properties": {k: v for k, v in raw.items() if k != "type"},
            }
        else:
            envelope = raw
            if "type" not in envelope:
                envelope = {**envelope, "type": event_type}
        events = self._normalizer.on_server_event(envelope)
        await self._emit_many(events)

    async def _handle_permission(self, props: dict[str, Any]) -> None:
        permission_id = str(props.get("permissionID") or props.get("id") or "")
        if not permission_id:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "permission event missing id")
        interaction_id = uuid4()
        self._pending_interactions[interaction_id] = permission_id
        events = self._normalizer.on_permission(
            permission_id=permission_id,
            tool=props.get("tool") if isinstance(props.get("tool"), str) else None,
            title=props.get("title") if isinstance(props.get("title"), str) else None,
            interaction_id=interaction_id,
        )
        for event in events:
            if isinstance(event, InteractionRequestedPayload):
                await self._event_q.put(
                    HarnessInteractionRequest(
                        payload=event,
                        provider_correlation={"permission_id": permission_id},
                    )
                )
            else:
                await self._event_q.put(event)

    async def _reconnect_resync(self) -> None:
        """Stub reconnect/resync after SSE drop while process alive."""
        if self._closed or self._client is None or self._session is None:
            return
        if self._process is not None and self._process.returncode is not None:
            events = self._normalizer.on_outcome_unknown("opencode process exited during SSE")
            await self._emit_many(events)
            return
        # Best-effort: reopen events and suppress duplicates via seen offsets.
        with contextlib.suppress(Exception):
            if self._sse_task is asyncio.current_task():
                self._sse_task = None
            await self._open_events()
            if self._session.native_session_id:
                response = await self._client.get(f"/session/{self._session.native_session_id}")
                if response.status_code != 200:
                    events = self._normalizer.on_outcome_unknown(
                        "opencode resync could not prove terminal state"
                    )
                    await self._emit_many(events)

    def _raise_http(self, response: Any, label: str) -> None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status >= 400:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                f"opencode {label} failed",
                details={"status_code": status},
            )

    async def _emit_many(self, events: list[HarnessEvent]) -> None:
        for event in events:
            await self._event_q.put(event)

    def _require_session(self, session: HarnessSession) -> None:
        if self._session is None:
            raise DomainError(ErrorCode.INVALID_STATE, "adapter has no active session")
        if self._closed:
            raise DomainError(ErrorCode.INVALID_STATE, "adapter is closed")
        if session.conversation_id != self._session.conversation_id:
            raise DomainError(ErrorCode.INVALID_STATE, "session conversation mismatch")
