"""Claude Code HarnessAdapter — SDK-managed ClaudeSDKClient per conversation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar, Literal, cast
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
from talktoharnesses.providers.claude.compatibility import (
    ClaudeReleaseRecord,
    enforce_published_operation,
)
from talktoharnesses.providers.claude.normalizer import ClaudeNormalizer
from talktoharnesses.providers.claude.probe import probe_claude
from talktoharnesses.providers.claude.schemas import parse_claude_message
from talktoharnesses.runtime.paths import resolve_executable

logger = logging.getLogger(__name__)

ClientFactory = Callable[[Any], Any]
_ClaudeEffort = Literal["low", "medium", "high", "max"]


def _claude_effort(value: str | None) -> _ClaudeEffort | None:
    return cast(_ClaudeEffort | None, value)


class ClaudeAdapter:
    """SDK-managed Claude adapter. One instance per conversation runtime."""

    kind: HarnessKind = HarnessKind.CLAUDE
    sdk_managed: ClassVar[Literal[True]] = True

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory
        self._client: Any | None = None
        self._options: Any | None = None
        self._response_task: asyncio.Task[None] | None = None
        self._normalizer = ClaudeNormalizer()
        self._release: ClaudeReleaseRecord | None = None
        self._capabilities: HarnessCapabilities | None = None
        self._session: HarnessSession | None = None
        self._event_q: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._pending_interactions: dict[UUID, asyncio.Future[InteractionAnswer]] = {}
        self._closed = False
        self._native_session_id: str | None = None

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
        caps, release = await probe_claude(config)
        self._capabilities = caps
        self._release = release
        return caps

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        if self._release is None:
            raise DomainError(ErrorCode.INVALID_STATE, "claude adapter must be probed before start")
        enforce_published_operation(self._release, mode="create")
        cwd = request.launch.working_directory or request.configuration.working_directory
        session_id = str(uuid4())
        await self._connect(
            request.configuration,
            cwd=cwd,
            resume=None,
            session_id=session_id,
        )
        self._native_session_id = session_id
        self._normalizer.set_session(session_id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CLAUDE,
            native_session_id=session_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
            effort=request.configuration.effort,
        )
        self._session = session
        return session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        if self._release is None:
            raise DomainError(
                ErrorCode.INVALID_STATE, "claude adapter must be probed before resume"
            )
        enforce_published_operation(self._release, mode="resume")
        cwd = request.launch.working_directory or request.configuration.working_directory
        self._normalizer.set_session(request.native_session_id, resync=True)
        await self._connect(
            request.configuration,
            cwd=cwd,
            resume=request.native_session_id,
            session_id=None,
        )
        session_id = request.native_session_id
        self._native_session_id = session_id
        self._normalizer.set_session(session_id, resync=False)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.CLAUDE,
            native_session_id=session_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
            effort=request.configuration.effort,
        )
        self._session = session
        return session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self._require_session(session)
        if self._client is None:
            raise DomainError(ErrorCode.INVALID_STATE, "claude client not connected")
        if self._response_task is not None and not self._response_task.done():
            raise DomainError(ErrorCode.INVALID_STATE, "claude turn already active")
        self._normalizer.begin_turn(request.turn_id)
        await self._client.query(request.prompt)
        self._response_task = asyncio.create_task(
            self._consume_response(),
            name=f"claude-response-{request.turn_id}",
        )

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        self._require_session(session)
        del request
        if self._release is None or not self._release.capabilities.supports_steer:
            return False
        enforce_published_operation(self._release, mode="steer")
        return False

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
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.interrupt()

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
            self._response_task is not None
            and self._response_task is not asyncio.current_task()
            and not self._response_task.done()
        ):
            self._response_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._response_task
        self._response_task = None
        if self._client is not None:
            disconnect = getattr(self._client, "disconnect", None)
            aexit = getattr(self._client, "__aexit__", None)
            if callable(disconnect):
                with contextlib.suppress(Exception):
                    result = disconnect()
                    if asyncio.iscoroutine(result):
                        await result
            elif callable(aexit):
                with contextlib.suppress(Exception):
                    result = aexit(None, None, None)
                    if asyncio.iscoroutine(result):
                        await result
            self._client = None
        for interaction_id, future in self._pending_interactions.items():
            if not future.done():
                future.set_result(
                    InteractionAnswer(
                        interaction_id=interaction_id,
                        decision=ApprovalDecision.CANCEL,
                    )
                )
        self._pending_interactions.clear()
        with contextlib.suppress(asyncio.QueueFull):
            self._event_q.put_nowait(None)

    async def _connect(
        self,
        config: HarnessConfiguration,
        *,
        cwd: str,
        resume: str | None,
        session_id: str | None,
    ) -> None:
        options = self._build_options(
            config,
            cwd=cwd,
            resume=resume,
            session_id=session_id,
        )
        self._options = options
        if self._client_factory is not None:
            client = self._client_factory(options)
        else:
            try:
                from claude_agent_sdk import ClaudeSDKClient
            except ImportError as exc:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "claude-agent-sdk extra is not installed",
                    details={"extra": "claude"},
                ) from exc
            client = ClaudeSDKClient(options=options)
        connect = getattr(client, "connect", None)
        aenter = getattr(client, "__aenter__", None)
        if callable(connect):
            result = connect()
            if asyncio.iscoroutine(result):
                await result
        elif callable(aenter):
            entered = aenter()
            if asyncio.iscoroutine(entered):
                client = await entered
        self._client = client

    def _build_options(
        self,
        config: HarnessConfiguration,
        *,
        cwd: str,
        resume: str | None,
        session_id: str | None,
    ) -> Any:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        except ImportError:
            # Fake factory path: return a simple namespace.
            options: dict[str, Any] = {
                "cwd": cwd,
                "model": config.model,
                "effort": config.effort,
                "resume": resume,
                "session_id": session_id,
                "permission_mode": "bypassPermissions" if config.yolo else "default",
                "cli_path": config.executable_path,
            }
            options["can_use_tool"] = (
                self._can_use_tool_yolo if config.yolo else self._can_use_tool
            )
            return options
        cli_path = None
        if config.executable_path:
            cli_path = str(resolve_executable(config.executable_path))

        if config.yolo:
            return ClaudeAgentOptions(
                cwd=cwd,
                model=config.model,
                effort=_claude_effort(config.effort),
                resume=resume,
                session_id=session_id,
                permission_mode="bypassPermissions",
                can_use_tool=self._can_use_tool_yolo,
                cli_path=cli_path,
                # Avoid project/local auto-allow settings; user auth still applies via SDK login.
                setting_sources=[],
            )

        async def _force_broker_ask(
            input_data: dict[str, Any],
            tool_use_id: str | None,
            context: Any,
        ) -> dict[str, Any]:
            del input_data, tool_use_id, context
            # Keep tool execution on the can_use_tool → answer_interaction path.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                }
            }

        return ClaudeAgentOptions(
            cwd=cwd,
            model=config.model,
            effort=_claude_effort(config.effort),
            resume=resume,
            session_id=session_id,
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            cli_path=cli_path,
            # Avoid project/local auto-allow settings; user auth still applies via SDK login.
            setting_sources=[],
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[cast(Any, _force_broker_ask)]),
                ]
            },
        )

    async def _consume_response(self) -> None:
        assert self._client is not None
        saw_result = False
        try:
            async for message in self._client.receive_response():
                raw = self._coerce_message(message)
                if raw is None:
                    continue
                try:
                    parsed = parse_claude_message(raw)
                except Exception as exc:
                    raise DomainError(
                        ErrorCode.UNSUPPORTED_NATIVE_EVENT,
                        f"unsupported claude message: {exc}",
                    ) from exc
                if raw.get("type") == "result":
                    saw_result = True
                events = self._normalizer.on_message(parsed)
                await self._emit_many(events)
            if not saw_result:
                raise DomainError(
                    ErrorCode.PROTOCOL_ERROR,
                    "claude response ended without ResultMessage",
                )
        except asyncio.CancelledError:
            return
        except DomainError as exc:
            logger.warning("claude response rejected: %s", exc.message)
            await self._emit_many(
                self._normalizer.fail_active_turn(
                    error_code=exc.code.value,
                    message=exc.message,
                )
            )
            # Protocol/schema faults make the response stream unusable. Publish
            # the terminal event first, then let the runtime close this adapter.
            await self._event_q.put(None)
        except Exception as exc:  # noqa: BLE001
            logger.exception("claude response consumer failed")
            await self._emit_many(
                self._normalizer.fail_active_turn(
                    error_code="provider_error",
                    message=str(exc),
                )
            )

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        interaction_id = uuid4()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InteractionAnswer] = loop.create_future()
        self._pending_interactions[interaction_id] = future
        tool_use_id = getattr(context, "tool_use_id", None)
        events = self._normalizer.on_permission_request(
            tool_name=tool_name,
            tool_input=tool_input,
            interaction_id=interaction_id,
            tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
        )
        await self._publish_interaction(events, tool_name=tool_name)
        answer = await future
        return self._to_permission_result(answer.decision)

    async def _can_use_tool_yolo(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        if tool_name != "AskUserQuestion":
            return self._to_permission_result(
                ApprovalDecision.ALLOW_ONCE,
                updated_input=tool_input,
            )
        interaction_id = uuid4()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InteractionAnswer] = loop.create_future()
        self._pending_interactions[interaction_id] = future
        del context
        questions_obj = tool_input.get("questions")
        questions: list[dict[str, Any]] = []
        if isinstance(questions_obj, list):
            for item in cast(list[object], questions_obj):
                if isinstance(item, dict):
                    questions.append(
                        {
                            str(key): value
                            for key, value in cast(dict[object, object], item).items()
                        }
                    )
        events = self._normalizer.on_question_request(
            questions=questions,
            interaction_id=interaction_id,
        )
        await self._publish_interaction(events, tool_name=tool_name)
        answer = await future
        if answer.decision is ApprovalDecision.CANCEL:
            return self._to_permission_result(ApprovalDecision.CANCEL)
        updated_input = {**tool_input, "answers": answer.answers or {}}
        return self._to_permission_result(
            ApprovalDecision.ALLOW_ONCE,
            updated_input=updated_input,
        )

    async def _publish_interaction(
        self,
        events: list[HarnessEvent],
        *,
        tool_name: str,
    ) -> None:
        for event in events:
            if isinstance(event, InteractionRequestedPayload):
                await self._event_q.put(
                    HarnessInteractionRequest(
                        payload=event,
                        provider_correlation={"tool_name": tool_name},
                    )
                )
            else:
                await self._event_q.put(event)

    def _to_permission_result(
        self,
        decision: ApprovalDecision | None,
        *,
        updated_input: dict[str, Any] | None = None,
    ) -> Any:
        try:
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        except ImportError:
            if decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_SESSION}:
                result: dict[str, Any] = {"behavior": "allow"}
                if updated_input is not None:
                    result["updatedInput"] = updated_input
                return result
            return {
                "behavior": "deny",
                "message": "denied",
                "interrupt": decision is ApprovalDecision.CANCEL,
            }
        if decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_SESSION}:
            return PermissionResultAllow(updated_input=updated_input)
        return PermissionResultDeny(
            message="denied by talktoharnesses",
            interrupt=decision is ApprovalDecision.CANCEL,
        )

    def _coerce_message(self, message: Any) -> dict[str, Any] | None:
        if isinstance(message, dict):
            return {str(k): v for k, v in cast(dict[object, object], message).items()}
        # Dataclass-style SDK messages (prefer explicit shapes over model_dump).
        name = type(message).__name__
        if name in {"RateLimitEvent"}:
            # Advisory SDK events; not part of the turn message model.
            return None
        if name == "ResultMessage":
            return {
                "type": "result",
                "subtype": getattr(message, "subtype", "success"),
                "session_id": getattr(message, "session_id", ""),
                "is_error": bool(getattr(message, "is_error", False)),
                "duration_ms": int(getattr(message, "duration_ms", 0) or 0),
                "duration_api_ms": int(getattr(message, "duration_api_ms", 0) or 0),
                "num_turns": int(getattr(message, "num_turns", 0) or 0),
                "stop_reason": getattr(message, "stop_reason", None),
                "total_cost_usd": getattr(message, "total_cost_usd", None),
                "usage": getattr(message, "usage", None),
                "result": getattr(message, "result", None),
                "errors": getattr(message, "errors", None),
            }
        if name == "AssistantMessage":
            content_out: list[dict[str, Any]] = []
            for block in getattr(message, "content", []) or []:
                bname = type(block).__name__
                if bname == "TextBlock":
                    content_out.append({"type": "text", "text": getattr(block, "text", "")})
                elif bname == "ThinkingBlock":
                    content_out.append(
                        {"type": "thinking", "thinking": getattr(block, "thinking", "")}
                    )
                elif bname == "ToolUseBlock":
                    content_out.append(
                        {
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": getattr(block, "input", {}) or {},
                        }
                    )
                elif bname == "ToolResultBlock":
                    content_out.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": getattr(block, "tool_use_id", ""),
                            "content": getattr(block, "content", None),
                            "is_error": getattr(block, "is_error", None),
                        }
                    )
            return {
                "type": "assistant",
                "content": content_out,
                "model": getattr(message, "model", "") or "",
                "session_id": getattr(message, "session_id", None),
                "message_id": getattr(message, "message_id", None),
            }
        if name == "SystemMessage":
            return {
                "type": "system",
                "subtype": getattr(message, "subtype", ""),
                "data": getattr(message, "data", {}) or {},
            }
        if name == "UserMessage":
            content_out: list[dict[str, Any]] = []
            raw_content = getattr(message, "content", []) or []
            if isinstance(raw_content, str):
                return {
                    "type": "user",
                    "content": raw_content,
                    "session_id": getattr(message, "session_id", None),
                }
            for block in raw_content:
                bname = type(block).__name__
                if bname == "TextBlock":
                    content_out.append({"type": "text", "text": getattr(block, "text", "")})
                elif bname == "ToolResultBlock":
                    content_out.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": getattr(block, "tool_use_id", ""),
                            "content": getattr(block, "content", None),
                            "is_error": getattr(block, "is_error", None),
                        }
                    )
            return {
                "type": "user",
                "content": content_out,
                "session_id": getattr(message, "session_id", None),
            }
        if hasattr(message, "model_dump"):
            dumped = message.model_dump()
            if isinstance(dumped, dict):
                return {str(k): v for k, v in cast(dict[object, object], dumped).items()}
        raise DomainError(
            ErrorCode.UNSUPPORTED_NATIVE_EVENT,
            "unrecognized claude message shape",
            details={"type": name},
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
