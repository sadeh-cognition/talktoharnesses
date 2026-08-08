"""Codex HarnessAdapter — SDK-managed AsyncCodex per conversation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar, Literal, cast

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import HarnessEvent
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
from talktoharnesses.providers.codex.compatibility import CodexReleaseRecord
from talktoharnesses.providers.codex.normalizer import CodexNormalizer
from talktoharnesses.providers.codex.probe import probe_codex
from talktoharnesses.providers.codex.schemas import parse_codex_notification

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], Any]


def _codex_settings(mode: str | None) -> tuple[Any, Any]:
    """Map finite canonical modes to tested Sandbox values."""
    try:
        from openai_codex import ApprovalMode, Sandbox
    except ImportError as exc:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "openai-codex extra is not installed",
            details={"extra": "codex"},
        ) from exc
    if mode in {None, "", "default", "workspace_write", "workspace-write"}:
        sandbox = Sandbox.workspace_write
    elif mode in {"read_only", "read-only"}:
        sandbox = Sandbox.read_only
    elif mode in {"full_access", "full-access"}:
        sandbox = Sandbox.full_access
    else:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "unsupported codex mode",
            details={"mode": mode},
        )
    return ApprovalMode.auto_review, sandbox


class CodexAdapter:
    """SDK-managed Codex adapter. One instance per conversation runtime."""

    kind: HarnessKind = HarnessKind.CODEX
    sdk_managed: ClassVar[Literal[True]] = True

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._client: Any | None = None
        self._thread: Any | None = None
        self._turn_handle: Any | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._normalizer = CodexNormalizer()
        self._release: CodexReleaseRecord | None = None
        self._capabilities: HarnessCapabilities | None = None
        self._session: HarnessSession | None = None
        self._event_q: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._closed = False

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

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        caps, release = await probe_codex(config)
        self._capabilities = caps
        self._release = release
        return caps

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        await self._ensure_client()
        assert self._client is not None
        cwd = request.launch.working_directory or request.configuration.working_directory
        approval_mode, sandbox = _codex_settings(request.configuration.mode)
        thread = await self._client.thread_start(
            cwd=cwd,
            model=request.configuration.model,
            sandbox=sandbox,
            approval_mode=approval_mode,
        )
        thread_id = str(getattr(thread, "id", "") or "")
        if not thread_id:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "codex thread_start missing id")
        self._thread = thread
        self._normalizer.set_session(thread_id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CODEX,
            native_session_id=thread_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
        )
        self._session = session
        return session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        await self._ensure_client()
        assert self._client is not None
        cwd = request.launch.working_directory or request.configuration.working_directory
        approval_mode, sandbox = _codex_settings(request.configuration.mode)
        self._normalizer.set_session(request.native_session_id, resync=True)
        thread = await self._client.thread_resume(
            request.native_session_id,
            cwd=cwd,
            model=request.configuration.model,
            sandbox=sandbox,
            approval_mode=approval_mode,
        )
        thread_id = str(getattr(thread, "id", "") or request.native_session_id)
        if thread_id != request.native_session_id:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "codex resume returned mismatched thread id",
                details={"expected": request.native_session_id, "got": thread_id},
            )
        self._thread = thread
        self._normalizer.set_session(thread_id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CODEX,
            native_session_id=thread_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
        )
        self._session = session
        return session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self._require_session(session)
        if self._thread is None:
            raise DomainError(ErrorCode.INVALID_STATE, "codex thread not started")
        if self._stream_task is not None and not self._stream_task.done():
            raise DomainError(ErrorCode.INVALID_STATE, "codex turn already active")
        self._normalizer.begin_turn(request.turn_id)
        handle = await self._thread.turn(request.prompt)
        self._turn_handle = handle
        self._stream_task = asyncio.create_task(
            self._consume_stream(handle),
            name=f"codex-stream-{request.turn_id}",
        )

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        self._require_session(session)
        if self._release is None or not self._release.capabilities.supports_steer:
            return False
        if self._turn_handle is None:
            return False
        try:
            await self._turn_handle.steer(request.prompt)
        except Exception as exc:  # noqa: BLE001
            # Explicit unsupported/not-active → queue; other failures propagate.
            message = str(exc).lower()
            if "not active" in message or "unsupported" in message:
                return False
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                f"codex steer failed: {exc}",
            ) from exc
        return True

    async def interrupt(self, session: HarnessSession) -> None:
        self._require_session(session)
        if self._turn_handle is not None:
            with contextlib.suppress(Exception):
                await self._turn_handle.interrupt()

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        self._require_session(session)
        del answer
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "the pinned Codex AsyncCodex SDK cannot answer brokered interactions",
        )

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
            self._stream_task is not None
            and self._stream_task is not asyncio.current_task()
            and not self._stream_task.done()
        ):
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stream_task
        self._stream_task = None
        self._turn_handle = None
        self._thread = None
        if self._client is not None:
            close = getattr(self._client, "close", None)
            aexit = getattr(self._client, "__aexit__", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            elif callable(aexit):
                with contextlib.suppress(Exception):
                    result = aexit(None, None, None)
                    if asyncio.iscoroutine(result):
                        await result
            self._client = None
        with contextlib.suppress(asyncio.QueueFull):
            self._event_q.put_nowait(None)

    async def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if self._client_factory is not None:
            client = self._client_factory()
        else:
            try:
                from openai_codex import AsyncCodex
            except ImportError as exc:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "openai-codex extra is not installed",
                    details={"extra": "codex"},
                ) from exc
            client = AsyncCodex()
        aenter = getattr(client, "__aenter__", None)
        if callable(aenter):
            entered = aenter()
            if asyncio.iscoroutine(entered):
                client = await entered
        else:
            start = getattr(client, "start", None)
            if callable(start):
                started = start()
                if asyncio.iscoroutine(started):
                    await started
        self._client = client

    async def _consume_stream(self, handle: Any) -> None:
        try:
            stream = handle.stream()
            async for event in stream:
                await self._handle_native_event(event)
        except asyncio.CancelledError:
            return
        except DomainError as exc:
            logger.warning("codex stream rejected: %s", exc.message)
            await self._emit_many(
                self._normalizer.fail_active_turn(
                    error_code=exc.code.value,
                    message=exc.message,
                )
            )
            await self._event_q.put(None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("codex stream failed")
            await self._emit_many(
                self._normalizer.fail_active_turn(
                    error_code="provider_error",
                    message=str(exc),
                )
            )
        finally:
            self._turn_handle = None

    async def _handle_native_event(self, event: Any) -> None:
        raw = self._coerce_notification(event)
        try:
            parsed = parse_codex_notification(raw)
        except Exception as exc:
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"unsupported codex notification: {exc}",
            ) from exc
        events = self._normalizer.on_notification(parsed)
        await self._emit_many(events)

    def _coerce_notification(self, event: Any) -> dict[str, Any]:
        if isinstance(event, dict):
            return {str(k): v for k, v in cast(dict[object, object], event).items()}
        method = getattr(event, "method", None)
        payload = getattr(event, "payload", None)
        payload_dump: dict[str, Any] | None = None
        if payload is not None and hasattr(payload, "model_dump"):
            dumped = payload.model_dump(mode="json")
            if isinstance(dumped, dict):
                payload_dump = {str(k): v for k, v in cast(dict[object, object], dumped).items()}
        if method in {"turn/started", "turn/completed"} and payload_dump is not None:
            turn_obj = payload_dump.get("turn")
            turn = (
                {str(k): v for k, v in cast(dict[object, object], turn_obj).items()}
                if isinstance(turn_obj, dict)
                else {}
            )
            if method == "turn/started":
                return {
                    "method": "turnStarted",
                    "thread_id": str(payload_dump.get("thread_id") or ""),
                    "turn_id": str(turn.get("id") or ""),
                }
            error_obj = turn.get("error")
            error = (
                {str(k): v for k, v in cast(dict[object, object], error_obj).items()}
                if isinstance(error_obj, dict)
                else {}
            )
            return {
                "method": "turnCompleted",
                "thread_id": str(payload_dump.get("thread_id") or ""),
                "turn_id": str(turn.get("id") or ""),
                "status": str(turn.get("status") or "completed"),
                "final_response": turn.get("final_response"),
                "error_message": error.get("message"),
            }
        if method == "item/agentMessage/delta" and payload_dump is not None:
            return {
                "method": "agentMessageDelta",
                "thread_id": str(payload_dump.get("thread_id") or ""),
                "turn_id": str(payload_dump.get("turn_id") or ""),
                "item_id": str(payload_dump.get("item_id") or ""),
                "delta": str(payload_dump.get("delta") or ""),
            }
        if (
            method
            in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
            }
            and payload_dump is not None
        ):
            return {
                "method": "reasoningDelta",
                "thread_id": str(payload_dump.get("thread_id") or ""),
                "turn_id": str(payload_dump.get("turn_id") or ""),
                "item_id": str(payload_dump.get("item_id") or ""),
                "delta": str(payload_dump.get("delta") or ""),
            }
        if method in {"item/started", "item/completed"} and payload_dump is not None:
            item_obj = payload_dump.get("item")
            item = (
                {str(k): v for k, v in cast(dict[object, object], item_obj).items()}
                if isinstance(item_obj, dict)
                else {}
            )
            native_type = str(item.get("type") or "")
            item_type = {
                "commandExecution": "command",
                "fileChange": "file",
                "mcpToolCall": "tool",
                "dynamicToolCall": "tool",
            }.get(native_type, native_type)
            raw: dict[str, Any] = {
                "method": "itemStarted" if method == "item/started" else "itemCompleted",
                "thread_id": str(payload_dump.get("thread_id") or ""),
                "turn_id": str(payload_dump.get("turn_id") or ""),
                "item_id": str(item.get("id") or ""),
                "item_type": item_type,
            }
            if method == "item/started":
                raw["title"] = item.get("tool") or item.get("command")
            else:
                raw["status"] = item.get("status")
            return raw
        # Fake SDK path: plain objects with model_dump / __dict__.
        if hasattr(event, "model_dump"):
            dumped = event.model_dump()
            if isinstance(dumped, dict):
                return {str(k): v for k, v in cast(dict[object, object], dumped).items()}
        data = getattr(event, "__dict__", None)
        if isinstance(data, dict) and "method" in data:
            return {str(k): v for k, v in cast(dict[object, object], data).items()}
        raise DomainError(
            ErrorCode.UNSUPPORTED_NATIVE_EVENT,
            "unrecognized codex notification shape",
            details={"type": type(event).__name__},
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
