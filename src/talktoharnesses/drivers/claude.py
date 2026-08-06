"""Claude harness — wraps ``claude-agent-sdk`` ClaudeSDKClient."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from talktoharnesses._event_bus import EventBus
from talktoharnesses.errors import ApprovalError, MissingDependencyError, SessionError
from talktoharnesses.events import (
    ContentDelta,
    ItemCompleted,
    ItemStarted,
    RequestOpened,
    RequestResolved,
    RuntimeEvent,
    RuntimeWarning,
    SessionExited,
    SessionStarted,
    ThreadStarted,
    TurnAborted,
    TurnCompleted,
    TurnStarted,
)
from talktoharnesses.types import (
    ApprovalDecision,
    Capabilities,
    SendTurnInput,
    Session,
    SessionStartInput,
)

PROVIDER = "claude"

ClientFactory = Callable[[Any], Any]
ContentKind = Literal["text", "reasoning", "command_output"]


class ClaudeHarness:
    """Harness over the official Claude Agent SDK."""

    name = PROVIDER
    capabilities = Capabilities(
        session_model_switch="in-session",
        interrupt_turn="in-session",
        approval="in-session",
        user_input="unsupported",
        resume_session="in-session",
    )

    def __init__(
        self,
        *,
        cwd: Path | str = ".",
        model: str | None = None,
        env: Mapping[str, str] | None = None,
        permission_mode: str | None = None,
        resume: str | None = None,
        fork_session: bool = False,
        setting_sources: list[str] | None = None,
        client_factory: ClientFactory | None = None,
        **_ignored: Any,
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.model = model
        self.env = dict(env or {})
        self.permission_mode = permission_mode
        self.resume = resume
        self.fork_session = fork_session
        self.setting_sources = setting_sources
        self._client_factory = client_factory

        self._client: Any = None
        self._bus = EventBus()
        self._session: Session | None = None
        self._session_id: str | None = None
        self._current_turn_id: str | None = None
        self._pending_approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        # turn_id -> content kinds already emitted from partial StreamEvents.
        self._streamed_kinds: dict[str, set[ContentKind]] = {}
        # tool_use_id -> turn_id, for tool calls awaiting their result.
        self._open_tool_calls: dict[str, str] = {}
        # Tools the user approved for the whole session. The SDK's permission
        # callback has no allow-always outcome, so we honour it on this side.
        self._session_allowed_tools: set[str] = set()
        self._closed = False
        self._connected = False

    async def start_session(self, input: SessionStartInput | None = None) -> Session:
        if self._session is not None:
            return self._session

        options = self._build_options(input)
        if self._client_factory is not None:
            self._client = self._client_factory(options)
        else:
            try:
                from claude_agent_sdk import ClaudeSDKClient
            except ImportError as exc:
                raise MissingDependencyError(
                    PROVIDER, "claude-agent-sdk", extra="claude"
                ) from exc
            self._client = ClaudeSDKClient(options=options)

        # Connect (no prompt yet).
        if hasattr(self._client, "connect"):
            await self._client.connect()
            self._connected = True

        session_id = str(
            getattr(self._client, "session_id", None)
            or (input.resume if input else None)
            or self.resume
            or uuid4()
        )
        self._session_id = session_id
        model = (input.model if input else None) or self.model
        self._session = Session(
            session_id=session_id,
            thread_id=session_id,
            provider=PROVIDER,
            model=model,
            started_at=datetime.now(UTC),
        )
        self._emit(
            SessionStarted(
                provider=PROVIDER,
                session_id=session_id,
                thread_id=session_id,
                model=model,
            )
        )
        self._emit(ThreadStarted(provider=PROVIDER, thread_id=session_id))
        return self._session

    def send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        return self._send_turn(prompt)

    async def _send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        if self._session is None or self._client is None:
            raise SessionError("start_session() must be called before send_turn()")

        text = prompt if isinstance(prompt, str) else prompt.prompt
        turn_id = str(uuid4())
        self._current_turn_id = turn_id

        if not isinstance(prompt, str) and prompt.model and hasattr(self._client, "set_model"):
            await self._client.set_model(prompt.model)

        q = self._bus.subscribe()
        started = TurnStarted(
            provider=PROVIDER,
            thread_id=self._session_id,
            turn_id=turn_id,
        )
        self._emit(started)
        yield started

        async def _run() -> None:
            try:
                await self._client.query(text)
                async for msg in self._client.receive_response():
                    self._handle_message(msg, turn_id)
                self._close_open_tool_calls(turn_id)
                self._emit(
                    TurnCompleted(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        stop_reason="end_turn",
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    TurnAborted(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        reason=str(exc),
                    )
                )
            finally:
                self._streamed_kinds.pop(turn_id, None)
                self._close_open_tool_calls(turn_id, status="incomplete")

        task = asyncio.create_task(_run())
        session_types = {
            "session.started",
            "session.configured",
            "session.exited",
            "thread.started",
        }
        try:
            async for event in self._bus.iter_queue(q):
                if (
                    event.type == "turn.started"
                    and event.turn_id == turn_id
                    and event.event_id == started.event_id
                ):
                    continue
                if (
                    event.turn_id
                    and event.turn_id != turn_id
                    and event.type not in session_types
                ):
                    continue
                yield event
                if event.type in ("turn.completed", "turn.aborted") and (
                    event.turn_id is None or event.turn_id == turn_id
                ):
                    break
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._bus.unsubscribe(q)

    def stream_events(self) -> AsyncIterator[RuntimeEvent]:
        return self._stream_events()

    async def _stream_events(self) -> AsyncIterator[RuntimeEvent]:
        q = self._bus.subscribe()
        async for event in self._bus.iter_queue(q):
            yield event

    async def interrupt_turn(self, turn_id: str | None = None) -> None:
        if self._client is None:
            raise SessionError("no active session")
        if hasattr(self._client, "interrupt"):
            await self._client.interrupt()
        self._emit(
            TurnAborted(
                provider=PROVIDER,
                thread_id=self._session_id,
                turn_id=turn_id or self._current_turn_id,
                reason="interrupted",
            )
        )

    async def respond(self, request_id: str, decision: ApprovalDecision) -> None:
        fut = self._pending_approvals.get(request_id)
        if fut is None or fut.done():
            raise ApprovalError(f"no open approval request {request_id!r}")
        fut.set_result(decision)

    async def respond_to_user_input(
        self,
        request_id: str,
        answers: Mapping[str, Any],
    ) -> None:
        raise ApprovalError("Claude harness does not support user-input prompts in v1")

    async def stop_session(self) -> None:
        if self._session is not None:
            self._emit(
                SessionExited(
                    provider=PROVIDER,
                    session_id=self._session.session_id,
                    thread_id=self._session_id,
                    reason="stopped",
                )
            )
        self._session = None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.close()
        for fut in self._pending_approvals.values():
            if not fut.done():
                fut.cancel()
        self._pending_approvals.clear()
        if self._client is not None:
            if hasattr(self._client, "disconnect"):
                with contextlib.suppress(Exception):
                    await self._client.disconnect()
            # Support async context manager clients
            if hasattr(self._client, "__aexit__"):
                with contextlib.suppress(Exception):
                    await self._client.__aexit__(None, None, None)
            self._client = None

    def _emit(self, event: RuntimeEvent) -> None:
        self._bus.publish(event)

    def _build_options(self, input: SessionStartInput | None) -> Any:
        try:
            from claude_agent_sdk import ClaudeAgentOptions
        except ImportError as exc:
            raise MissingDependencyError(
                PROVIDER, "claude-agent-sdk", extra="claude"
            ) from exc

        model = (input.model if input else None) or self.model
        resume = (input.resume if input else None) or self.resume
        kwargs: dict[str, Any] = {
            "cwd": str(self.cwd),
            "include_partial_messages": True,
            "can_use_tool": self._can_use_tool,
        }
        if model:
            kwargs["model"] = model
        if self.permission_mode:
            kwargs["permission_mode"] = self.permission_mode
        if resume:
            kwargs["resume"] = resume
        if self.fork_session:
            kwargs["fork_session"] = True
        if self.setting_sources:
            kwargs["setting_sources"] = self.setting_sources
        if self.env:
            kwargs["env"] = self.env
        return ClaudeAgentOptions(**kwargs)

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any = None,
    ) -> Any:
        try:
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        except ImportError as exc:
            raise MissingDependencyError(
                PROVIDER, "claude-agent-sdk", extra="claude"
            ) from exc

        if tool_name in self._session_allowed_tools:
            # Already approved for this session — don't re-prompt.
            return PermissionResultAllow(behavior="allow")

        request_id = str(uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending_approvals[request_id] = fut

        self._emit(
            RequestOpened(
                provider=PROVIDER,
                thread_id=self._session_id,
                turn_id=self._current_turn_id,
                request_id=request_id,
                request_type="dynamic_tool_call",
                title=tool_name,
                tool_name=tool_name,
                detail=str(tool_input) if tool_input else None,
                raw={"tool_name": tool_name, "tool_input": tool_input},
            )
        )
        try:
            decision = await fut
        finally:
            self._pending_approvals.pop(request_id, None)

        if decision == "decline":
            self._emit(
                RequestResolved(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=self._current_turn_id,
                    request_id=request_id,
                    decision="deny",
                )
            )
            return PermissionResultDeny(behavior="deny", message="declined by user")

        native = "allow" if decision == "accept" else "allow_for_session"
        if decision == "accept_for_session":
            self._session_allowed_tools.add(tool_name)
        self._emit(
            RequestResolved(
                provider=PROVIDER,
                thread_id=self._session_id,
                turn_id=self._current_turn_id,
                request_id=request_id,
                decision=native,
            )
        )
        return PermissionResultAllow(behavior="allow")

    def _handle_message(self, msg: Any, turn_id: str) -> None:
        cls_name = type(msg).__name__

        if cls_name == "AssistantMessage" or hasattr(msg, "content"):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    self._handle_block(block, turn_id, msg)
            return

        if cls_name == "StreamEvent" or (hasattr(msg, "event") and hasattr(msg, "session_id")):
            event = getattr(msg, "event", {}) or {}
            extracted = _extract_stream_delta(event)
            if extracted is not None:
                text, kind = extracted
                # Record that this turn streamed this kind, so the assembled
                # AssistantMessage that follows is not emitted a second time.
                self._streamed_kinds.setdefault(turn_id, set()).add(kind)
                self._emit(
                    ContentDelta(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        text=text,
                        content_kind=kind,
                        raw=event if isinstance(event, dict) else {"event": repr(event)},
                    )
                )
            return

        if cls_name == "ResultMessage":
            # turn.completed is emitted after the iterator ends; surface errors.
            if getattr(msg, "is_error", False):
                self._emit(
                    RuntimeWarning(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        message=str(getattr(msg, "result", None) or "result error"),
                        code="result_error",
                        raw=_maybe_dump(msg),
                    )
                )
            return

        self._emit(
            RuntimeWarning(
                provider=PROVIDER,
                thread_id=self._session_id,
                turn_id=turn_id,
                message=f"unhandled Claude message: {cls_name}",
                code="unhandled_claude_message",
                raw=_maybe_dump(msg),
            )
        )

    def _close_open_tool_calls(self, turn_id: str, *, status: str = "completed") -> None:
        """Complete any tool call whose result never arrived.

        A turn that ends (or is aborted) with tool calls still open would
        otherwise leave consumers holding ``item.started`` events forever.
        """
        stranded = [tid for tid, tturn in self._open_tool_calls.items() if tturn == turn_id]
        for tool_use_id in stranded:
            del self._open_tool_calls[tool_use_id]
            self._emit(
                ItemCompleted(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=turn_id,
                    item_id=tool_use_id,
                    item_type="dynamic_tool_call",
                    status=status,
                )
            )

    def _streamed(self, turn_id: str, kind: ContentKind) -> bool:
        """True if this turn already streamed ``kind`` via partial messages.

        With ``include_partial_messages=True`` the SDK yields both the partial
        ``StreamEvent`` deltas *and* the assembled ``AssistantMessage``. Emitting
        both would double every token for anyone concatenating ``content.delta``.
        The partials win; the assembled message is used only as a fallback when
        partials did not arrive.
        """
        return kind in self._streamed_kinds.get(turn_id, frozenset())

    def _handle_block(self, block: Any, turn_id: str, parent: Any) -> None:
        bname = type(block).__name__
        if bname == "TextBlock" or (hasattr(block, "text") and not hasattr(block, "name")):
            text = getattr(block, "text", None)
            if text and not self._streamed(turn_id, "text"):
                self._emit(
                    ContentDelta(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        text=str(text),
                        content_kind="text",
                        item_id=getattr(parent, "message_id", None),
                    )
                )
            return
        if bname == "ThinkingBlock":
            thinking = getattr(block, "thinking", None) or getattr(block, "text", None)
            if thinking and not self._streamed(turn_id, "reasoning"):
                self._emit(
                    ContentDelta(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        text=str(thinking),
                        content_kind="reasoning",
                    )
                )
            return
        if bname == "ToolUseBlock" or (hasattr(block, "name") and hasattr(block, "id")):
            # A tool call has only *started* here. Completion arrives later as a
            # ToolResultBlock; emitting item.completed now would tell consumers
            # every tool finishes instantly.
            tool_use_id = str(getattr(block, "id", "") or uuid4())
            self._open_tool_calls[tool_use_id] = turn_id
            self._emit(
                ItemStarted(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=turn_id,
                    item_id=tool_use_id,
                    item_type="dynamic_tool_call",
                    title=str(getattr(block, "name", "") or "tool"),
                )
            )
            return
        if bname == "ToolResultBlock" or hasattr(block, "tool_use_id"):
            tool_use_id = str(getattr(block, "tool_use_id", "") or "")
            self._open_tool_calls.pop(tool_use_id, None)
            is_error = bool(getattr(block, "is_error", False))
            self._emit(
                ItemCompleted(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=turn_id,
                    item_id=tool_use_id or None,
                    item_type="dynamic_tool_call",
                    status="error" if is_error else "completed",
                    detail=_result_detail(block),
                )
            )


#: Anthropic stream delta type -> canonical ContentDelta.content_kind.
_STREAM_DELTA_KINDS: dict[str, ContentKind] = {
    "text_delta": "text",
    "thinking_delta": "reasoning",
}


def _extract_stream_delta(event: Any) -> tuple[str, ContentKind] | None:
    """Pull ``(text, content_kind)`` out of an Anthropic stream event.

    Only assistant-visible text and reasoning are returned. ``partial_json``
    deltas carry streamed *tool arguments*, not prose — folding those into
    ``content.delta`` would corrupt any consumer concatenating the stream.
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    kind = _STREAM_DELTA_KINDS.get(str(delta.get("type") or ""))
    if kind is None:
        return None
    text = delta.get("text") if kind == "text" else delta.get("thinking")
    if not text:
        return None
    return str(text), kind


def _result_detail(block: Any, *, max_chars: int = 500) -> str | None:
    content = getattr(block, "content", None)
    if content is None:
        return None
    text = content if isinstance(content, str) else repr(content)
    text = text.strip()
    if not text:
        return None
    return text[:max_chars] + "…" if len(text) > max_chars else text


def _maybe_dump(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return {k: repr(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return {"repr": repr(obj)}


def create_claude_harness(**kwargs: Any) -> ClaudeHarness:
    return ClaudeHarness(**kwargs)
