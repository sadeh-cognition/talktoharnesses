"""Codex HarnessAdapter — SDK-managed AsyncCodex per conversation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar, Literal, cast
from uuid import UUID, uuid4

from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import HarnessEvent, InteractionRequestedPayload
from talktoharnesses.domain.models import (
    CanonicalQuestion,
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
)
from talktoharnesses.domain.questions import canonical_answer_values, canonical_questions
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.codex.compatibility import (
    CodexReleaseRecord,
    enforce_published_operation,
)
from talktoharnesses.providers.codex.normalizer import CodexNormalizer
from talktoharnesses.providers.codex.probe import probe_codex
from talktoharnesses.providers.codex.schemas import (
    CodexUserInputParams,
    parse_codex_notification,
    parse_codex_server_request_params,
)

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], Any]

_INTERACTION_TIMEOUT_S = 3600.0


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
    # ApprovalMode.auto_review pairs on-request with ApprovalsReviewer.auto_review,
    # which hides brokered approvals. BrokerAsyncCodex forces ApprovalsReviewer.user.
    return ApprovalMode.auto_review, sandbox


def _sandbox_wire_value(sandbox: Any) -> Any:
    """Map openai_codex.Sandbox (or wire string) to protocol SandboxMode."""
    from openai_codex.generated.v2_all import SandboxMode

    value = str(getattr(sandbox, "value", sandbox))
    if value in {"full-access", "full_access", "danger-full-access", "danger_full_access"}:
        return SandboxMode.danger_full_access
    return SandboxMode(value)


def _codex_approval_params(*, yolo: bool) -> dict[str, Any]:
    """Map yolo to Codex approval_policy. Sandbox is selected separately."""
    from openai_codex.generated.v2_all import (
        ApprovalsReviewer,
        AskForApproval,
        AskForApprovalValue,
    )

    if yolo:
        return {"approval_policy": AskForApproval(root=AskForApprovalValue.never)}
    return {
        "approval_policy": AskForApproval(root=AskForApprovalValue.on_request),
        "approvals_reviewer": ApprovalsReviewer.user,
    }


def _build_broker_async_codex(
    approval_handler: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None,
    *,
    yolo: bool = False,
) -> Any:
    """Build AsyncCodex with public CodexClient and brokered or yolo approvals."""
    from openai_codex import AsyncCodex, AsyncThread, CodexConfig
    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.client import CodexClient
    from openai_codex.generated.v2_all import (
        ThreadResumeParams,
        ThreadStartParams,
    )

    class BrokerAsyncCodexClient(AsyncCodexClient):
        def __init__(self, config: Any = None) -> None:
            # Public CodexClient accepts approval_handler; AsyncCodexClient does not forward it.
            self._sync = CodexClient(config=config, approval_handler=approval_handler)

    class BrokerAsyncCodex(AsyncCodex):
        def __init__(self, config: Any = None) -> None:
            self._client = BrokerAsyncCodexClient(config=config)
            self._init = None
            self._initialized = False
            self._init_lock = asyncio.Lock()

        async def thread_start(self, **kwargs: Any) -> Any:
            await self._ensure_initialized()
            sandbox = kwargs.get("sandbox")
            params = ThreadStartParams(
                **_codex_approval_params(yolo=yolo),
                cwd=kwargs.get("cwd"),
                model=kwargs.get("model"),
                sandbox=_sandbox_wire_value(sandbox) if sandbox is not None else None,
            )
            started = await self._client.thread_start(params)
            return AsyncThread(self, started.thread.id)

        async def thread_resume(self, thread_id: str, **kwargs: Any) -> Any:
            await self._ensure_initialized()
            sandbox = kwargs.get("sandbox")
            params = ThreadResumeParams(
                thread_id=thread_id,
                **_codex_approval_params(yolo=yolo),
                cwd=kwargs.get("cwd"),
                model=kwargs.get("model"),
                sandbox=_sandbox_wire_value(sandbox) if sandbox is not None else None,
            )
            await self._client.thread_resume(thread_id, params)
            return AsyncThread(self, thread_id)

    return BrokerAsyncCodex(
        CodexConfig(config_overrides=("features.default_mode_request_user_input=true",))
    )


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
        self._pending_interactions: dict[UUID, asyncio.Future[InteractionAnswer]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
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
        if self._release is None:
            raise DomainError(ErrorCode.INVALID_STATE, "codex adapter must be probed before start")
        enforce_published_operation(self._release, mode="create")
        await self._ensure_client(yolo=request.configuration.yolo)
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
            effort=request.configuration.effort,
        )
        self._session = session
        return session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        if self._release is None:
            raise DomainError(ErrorCode.INVALID_STATE, "codex adapter must be probed before resume")
        enforce_published_operation(self._release, mode="resume")
        await self._ensure_client(yolo=request.configuration.yolo)
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
            effort=request.configuration.effort,
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
        turn_options: dict[str, Any] = {}
        if session.effort:
            from openai_codex.types import ReasoningEffort

            turn_options["effort"] = ReasoningEffort(session.effort)
        handle = await self._thread.turn(request.prompt, **turn_options)
        self._turn_handle = handle
        self._stream_task = asyncio.create_task(
            self._consume_stream(handle),
            name=f"codex-stream-{request.turn_id}",
        )

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        self._require_session(session)
        if self._release is None or not self._release.capabilities.supports_steer:
            return False
        enforce_published_operation(self._release, mode="steer")
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
        if self._release is not None and self._release.capabilities.supports_interrupt:
            enforce_published_operation(self._release, mode="interrupt")
        for interaction_id, future in list(self._pending_interactions.items()):
            if not future.done():
                future.set_result(
                    InteractionAnswer(
                        interaction_id=interaction_id,
                        decision=ApprovalDecision.CANCEL,
                    )
                )
            del self._pending_interactions[interaction_id]
        if self._turn_handle is not None:
            with contextlib.suppress(Exception):
                await self._turn_handle.interrupt()

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        self._require_session(session)
        pending = self._pending_interactions.get(answer.interaction_id)
        if pending is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "no pending interaction for answer",
                details={"interaction_id": str(answer.interaction_id)},
            )
        del self._pending_interactions[answer.interaction_id]
        if not pending.done():
            pending.set_result(answer)

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
        for interaction_id, future in self._pending_interactions.items():
            if not future.done():
                future.set_result(
                    InteractionAnswer(
                        interaction_id=interaction_id,
                        decision=ApprovalDecision.CANCEL,
                    )
                )
        self._pending_interactions.clear()
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

    async def _ensure_client(self, *, yolo: bool = False) -> None:
        if self._client is not None:
            return
        self._loop = asyncio.get_running_loop()
        if self._client_factory is not None:
            client = self._client_factory()
        else:
            try:
                client = _build_broker_async_codex(
                    self._approval_handler,
                    yolo=yolo,
                )
            except ImportError as exc:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "openai-codex extra is not installed",
                    details={"extra": "codex"},
                ) from exc
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

    def _approval_handler(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Broker a blocking Codex server request through the interaction API."""
        loop = self._loop
        if loop is None or loop.is_closed():
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "codex approval handler has no event loop",
            )
        bridge: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()

        async def _emit_and_wait() -> dict[str, Any]:
            try:
                parsed = parse_codex_server_request_params(method, params)
            except Exception as exc:
                raise DomainError(
                    ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                    f"unsupported codex server request: {exc}",
                ) from exc
            interaction_id = uuid4()
            future: asyncio.Future[InteractionAnswer] = asyncio.get_running_loop().create_future()
            self._pending_interactions[interaction_id] = future
            questions: tuple[CanonicalQuestion, ...] | None = None
            if isinstance(parsed, CodexUserInputParams):
                questions = canonical_questions(
                    [item.model_dump(by_alias=True, exclude_none=True) for item in parsed.questions]
                )
                events = self._normalizer.on_user_input_request(
                    questions=questions,
                    interaction_id=interaction_id,
                )
            else:
                events = self._normalizer.on_approval_request(
                    method=method,
                    params=parsed,
                    interaction_id=interaction_id,
                )
            for event in events:
                if isinstance(event, InteractionRequestedPayload):
                    item_id = getattr(parsed, "item_id", None)
                    correlation: dict[str, str] = {"method": method}
                    if item_id is not None:
                        correlation["item_id"] = str(item_id)
                    await self._event_q.put(
                        HarnessInteractionRequest(
                            payload=event,
                            provider_correlation=correlation,
                        )
                    )
                else:
                    await self._event_q.put(event)
            answer = await future
            if questions is not None:
                return self._to_user_input_result(answer, questions)
            return self._to_approval_result(method, answer.decision)

        def _done(task: concurrent.futures.Future[dict[str, Any]]) -> None:
            if bridge.done():
                return
            try:
                bridge.set_result(task.result())
            except Exception as exc:  # noqa: BLE001
                bridge.set_exception(exc)

        asyncio.run_coroutine_threadsafe(_emit_and_wait(), loop).add_done_callback(_done)
        try:
            return bridge.result(timeout=_INTERACTION_TIMEOUT_S)
        except concurrent.futures.TimeoutError as exc:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "codex interaction wait timed out",
            ) from exc

    def _to_user_input_result(
        self,
        answer: InteractionAnswer,
        questions: tuple[CanonicalQuestion, ...],
    ) -> dict[str, Any]:
        values = canonical_answer_values(answer, questions)
        return {
            "answers": {
                question_id: {"answers": selected} for question_id, selected in values.items()
            }
        }

    def _to_approval_result(
        self,
        method: str,
        decision: ApprovalDecision | None,
    ) -> dict[str, Any]:
        del method
        if decision is ApprovalDecision.ALLOW_ONCE:
            return {"decision": "accept"}
        if decision is ApprovalDecision.ALLOW_SESSION:
            return {"decision": "acceptForSession"}
        if decision is ApprovalDecision.CANCEL:
            return {"decision": "cancel"}
        return {"decision": "decline"}

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
        if raw is None:
            return
        try:
            parsed = parse_codex_notification(raw)
        except Exception as exc:
            raise DomainError(
                ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                f"unsupported codex notification: {exc}",
            ) from exc
        events = self._normalizer.on_notification(parsed)
        await self._emit_many(events)

    def _coerce_notification(self, event: Any) -> dict[str, Any] | None:
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
        if isinstance(method, str) and "/" in method:
            # SDK emits many advisory notifications; ignore ones outside the turn model.
            logger.debug("ignoring codex notification method=%s", method)
            return None
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
