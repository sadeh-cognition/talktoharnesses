"""Codex harness driver — spawns ``codex app-server`` over stdio JSON-RPC."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from talktoharnesses._event_bus import EventBus
from talktoharnesses.codex.client import CodexAppServerClient
from talktoharnesses.codex.methods import Notifications, ServerRequests
from talktoharnesses.errors import ApprovalError, ProcessError, SessionError
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
    ThreadTokenUsageUpdated,
    TurnAborted,
    TurnCompleted,
    TurnDiffUpdated,
    TurnPlanUpdated,
    TurnStarted,
    UserInputRequested,
    UserInputResolved,
)
from talktoharnesses.transports.process import ManagedProcess, spawn_process
from talktoharnesses.transports.stdio_jsonrpc import JsonRpcPeer
from talktoharnesses.types import (
    ApprovalDecision,
    CanonicalItemType,
    Capabilities,
    DiffHunk,
    PlanStep,
    SendTurnInput,
    Session,
    SessionStartInput,
    TokenUsage,
)

PROVIDER = "codex"

_DECISION_MAP: dict[str, str] = {
    "accept": "accept",
    "accept_for_session": "acceptForSession",
    "decline": "decline",
}


def _as_dict(params: Any) -> dict[str, Any]:
    return dict(params) if isinstance(params, Mapping) else {}


#: Codex item ``type`` -> canonical item type. Verified against the vendored
#: schema by ``tests/test_codex_schema_drift.py``; upstream types with no
#: canonical equivalent (hookPrompt, imageGeneration, sleep, subAgentActivity)
#: deliberately fall through to ``unknown`` with their payload in ``raw``.
_ITEM_TYPES: dict[str, CanonicalItemType] = {
    "agentMessage": "assistant_message",
    "userMessage": "user_message",
    "reasoning": "reasoning",
    "plan": "plan",
    "commandExecution": "command_execution",
    "fileChange": "file_change",
    "mcpToolCall": "mcp_tool_call",
    "dynamicToolCall": "dynamic_tool_call",
    "collabAgentToolCall": "collab_agent_tool_call",
    "webSearch": "web_search",
    "imageView": "image_view",
    "enteredReviewMode": "review_entered",
    "exitedReviewMode": "review_exited",
    "contextCompaction": "context_compaction",
}


def _item_type(raw: str | None) -> CanonicalItemType:
    if raw is None:
        return "unknown"
    return _ITEM_TYPES.get(raw, "unknown")


class CodexHarness:
    """Harness implementation for the Codex app-server protocol."""

    name = PROVIDER
    capabilities = Capabilities(
        session_model_switch="unsupported",
        interrupt_turn="in-session",
        approval="in-session",
        user_input="in-session",
        resume_session="in-session",
    )

    def __init__(
        self,
        *,
        cwd: Path | str = ".",
        model: str | None = None,
        binary: str | None = None,
        env: Mapping[str, str] | None = None,
        extra_args: Sequence[str] | None = None,
        codex_home: str | Path | None = None,
        command: Sequence[str] | None = None,
        **_ignored: Any,
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.model = model
        self.binary = binary or "codex"
        self.env = dict(env or {})
        self.extra_args = list(extra_args or [])
        self.codex_home = Path(codex_home).resolve() if codex_home else None
        # ``command`` overrides the full spawn argv (used by mock-peer tests).
        self._command_override = list(command) if command is not None else None

        self._proc: ManagedProcess | None = None
        self._client: CodexAppServerClient | None = None
        self._bus = EventBus()
        self._session: Session | None = None
        self._thread_id: str | None = None
        self._current_turn_id: str | None = None
        self._pending_approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._pending_user_input: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_session(self, input: SessionStartInput | None = None) -> Session:
        if self._session is not None:
            return self._session

        await self._ensure_process()
        assert self._client is not None

        try:
            await self._client.initialize(
                {
                    "clientInfo": {
                        "name": "talktoharnesses",
                        "version": "0.1.0",
                    }
                }
            )
            await self._client.initialized()
        except Exception as exc:
            # A handshake failure is nearly always the CLI refusing to start
            # (not logged in, bad CODEX_HOME, wrong version). It says so on
            # stderr, so surface that instead of a bare timeout.
            raise self._startup_error("codex app-server handshake failed", exc) from exc

        start_params: dict[str, Any] = {"cwd": str(self.cwd)}
        model = (input.model if input else None) or self.model
        if model:
            start_params["model"] = model
        if input and input.resume:
            result = await self._client.thread_resume(
                {"threadId": input.resume, **start_params}
            )
        else:
            result = await self._client.thread_start(start_params)

        thread = _as_dict(_as_dict(result).get("thread"))
        thread_id = str(thread.get("id") or thread.get("sessionId") or uuid4())
        self._thread_id = thread_id
        session_id = str(thread.get("sessionId") or thread_id)
        resolved_model = _as_dict(result).get("model") or model

        self._session = Session(
            session_id=session_id,
            thread_id=thread_id,
            provider=PROVIDER,
            model=str(resolved_model) if resolved_model else None,
            started_at=datetime.now(UTC),
        )
        self._emit(
            SessionStarted(
                provider=PROVIDER,
                session_id=session_id,
                thread_id=thread_id,
                model=self._session.model,
                raw=_as_dict(result),
            )
        )
        self._emit(
            ThreadStarted(
                provider=PROVIDER,
                thread_id=thread_id,
                raw=thread or None,
            )
        )
        return self._session

    def send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        return self._send_turn(prompt)

    async def _send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        if self._session is None or self._client is None or self._thread_id is None:
            raise SessionError("start_session() must be called before send_turn()")

        text = prompt if isinstance(prompt, str) else prompt.prompt
        q = self._bus.subscribe()
        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if not isinstance(prompt, str) and prompt.model:
            params["model"] = prompt.model

        result = await self._client.turn_start(params)
        turn = _as_dict(_as_dict(result).get("turn"))
        turn_id = str(turn.get("id") or uuid4())
        self._current_turn_id = turn_id

        # Yield TurnStarted first. Notifications from the peer may already be
        # queued (they race with the turn/start response), so publishing
        # TurnStarted onto the bus can place it *after* turn.completed.
        started = TurnStarted(
            provider=PROVIDER,
            thread_id=self._thread_id,
            turn_id=turn_id,
            raw=_as_dict(result),
        )
        self._emit(started)
        yield started

        session_types = {
            "session.started",
            "session.configured",
            "session.exited",
            "thread.started",
        }
        try:
            async for event in self._bus.iter_queue(q):
                # Skip the TurnStarted we already yielded (if the bus copy arrives).
                if (
                    event.type == "turn.started"
                    and event.turn_id == turn_id
                    and event.event_id == started.event_id
                ):
                    continue
                # Filtered view of this turn (session-level events always pass).
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
            self._bus.unsubscribe(q)

    def stream_events(self) -> AsyncIterator[RuntimeEvent]:
        return self._stream_events()

    async def _stream_events(self) -> AsyncIterator[RuntimeEvent]:
        q = self._bus.subscribe()
        async for event in self._bus.iter_queue(q):
            yield event

    async def interrupt_turn(self, turn_id: str | None = None) -> None:
        if self._client is None or self._thread_id is None:
            raise SessionError("no active session")
        tid = turn_id or self._current_turn_id
        params: dict[str, Any] = {"threadId": self._thread_id}
        if tid:
            params["turnId"] = tid
        await self._client.turn_interrupt(params)

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
        fut = self._pending_user_input.get(request_id)
        if fut is None or fut.done():
            raise ApprovalError(f"no open user-input request {request_id!r}")
        fut.set_result(dict(answers))

    async def stop_session(self) -> None:
        if self._session is not None:
            self._emit(
                SessionExited(
                    provider=PROVIDER,
                    session_id=self._session.session_id,
                    thread_id=self._thread_id,
                    reason="stopped",
                )
            )
        self._session = None
        self._thread_id = None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.close()
        pending: list[asyncio.Future[Any]] = [
            *self._pending_approvals.values(),
            *self._pending_user_input.values(),
        ]
        for fut in pending:
            if not fut.done():
                fut.cancel()
        self._pending_approvals.clear()
        self._pending_user_input.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._proc is not None:
            await self._proc.aclose(timeout=5.0)
            self._proc = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _emit(self, event: RuntimeEvent) -> None:
        self._bus.publish(event)

    def _startup_error(self, message: str, cause: Exception) -> ProcessError:
        """Build a ProcessError carrying the child's stderr tail, if any."""
        tail = self._proc.stderr_tail() if self._proc is not None else ""
        returncode = self._proc.returncode if self._proc is not None else None
        detail = f"{message}: {cause}"
        if returncode is not None:
            detail += f" (exited with code {returncode})"
        if tail:
            detail += f"\n--- {self.binary} stderr ---\n{tail}"
        return ProcessError(detail, returncode=returncode, stderr=tail or None)

    async def _ensure_process(self) -> None:
        if self._client is not None:
            return

        if self._command_override is not None:
            cmd = list(self._command_override)
        else:
            cmd = [self.binary, "app-server", *self.extra_args]

        env = {**self.env}
        if self.codex_home is not None:
            env["CODEX_HOME"] = str(self.codex_home)

        self._proc = await spawn_process(cmd, cwd=self.cwd, env=env or None)
        assert self._proc.stdout is not None and self._proc.stdin is not None
        peer = JsonRpcPeer(reader=self._proc.stdout, writer=self._proc.stdin)
        self._client = CodexAppServerClient(peer)
        self._client.on_notification(self._on_notification)
        self._client.register_approval_handlers(
            on_command_approval=self._handle_approval_request,
            on_file_change_approval=self._handle_approval_request,
            on_user_input=self._handle_user_input_request,
            on_permissions_approval=self._handle_approval_request,
        )
        self._client.start()

    async def _on_notification(self, method: str, params: Any) -> None:
        p = _as_dict(params)
        thread_id = p.get("threadId") or self._thread_id
        turn = _as_dict(p.get("turn"))
        turn_id = p.get("turnId") or turn.get("id") or self._current_turn_id
        item = _as_dict(p.get("item"))
        item_id = item.get("id") or p.get("itemId")

        if method == Notifications.TURN_STARTED:
            tid = str(turn.get("id") or turn_id or "")
            if tid:
                self._current_turn_id = tid
            # Avoid double TurnStarted if send_turn already emitted one.
            return

        if method == Notifications.TURN_COMPLETED:
            status = turn.get("status")
            if status == "aborted" or status == "interrupted":
                self._emit(
                    TurnAborted(
                        provider=PROVIDER,
                        thread_id=str(thread_id) if thread_id else None,
                        turn_id=str(turn_id) if turn_id else None,
                        reason=str(status),
                        raw=p,
                    )
                )
            else:
                self._emit(
                    TurnCompleted(
                        provider=PROVIDER,
                        thread_id=str(thread_id) if thread_id else None,
                        turn_id=str(turn_id) if turn_id else None,
                        stop_reason=str(status) if status else "completed",
                        raw=p,
                    )
                )
            return

        if method == Notifications.ITEM_AGENT_MESSAGE_DELTA:
            delta = p.get("delta") or p.get("text") or ""
            self._emit(
                ContentDelta(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    item_id=str(item_id) if item_id else None,
                    text=str(delta),
                    content_kind="text",
                    raw=p,
                )
            )
            return

        if method in (
            Notifications.ITEM_REASONING_TEXT_DELTA,
            Notifications.ITEM_REASONING_SUMMARY_TEXT_DELTA,
        ):
            delta = p.get("delta") or p.get("text") or ""
            self._emit(
                ContentDelta(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    item_id=str(item_id) if item_id else None,
                    text=str(delta),
                    content_kind="reasoning",
                    raw=p,
                )
            )
            return

        if method == Notifications.ITEM_COMMAND_EXECUTION_OUTPUT_DELTA:
            delta = p.get("delta") or p.get("text") or ""
            self._emit(
                ContentDelta(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    item_id=str(item_id) if item_id else None,
                    text=str(delta),
                    content_kind="command_output",
                    raw=p,
                )
            )
            return

        if method == Notifications.ITEM_STARTED:
            raw_type = item.get("type")
            self._emit(
                ItemStarted(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    item_id=str(item_id) if item_id else None,
                    item_type=_item_type(str(raw_type) if raw_type else None),
                    title=item.get("title") or item.get("command"),
                    raw=p,
                )
            )
            return

        if method == Notifications.ITEM_COMPLETED:
            raw_type = item.get("type")
            self._emit(
                ItemCompleted(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    item_id=str(item_id) if item_id else None,
                    item_type=_item_type(str(raw_type) if raw_type else None),
                    status=str(item.get("status")) if item.get("status") else None,
                    raw=p,
                )
            )
            return

        if method == Notifications.THREAD_TOKEN_USAGE_UPDATED:
            usage_raw = _as_dict(p.get("tokenUsage"))
            total = _as_dict(usage_raw.get("total") or usage_raw)
            self._emit(
                ThreadTokenUsageUpdated(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    usage=TokenUsage(
                        input_tokens=_as_int(total.get("inputTokens")),
                        output_tokens=_as_int(total.get("outputTokens")),
                        total_tokens=_as_int(total.get("totalTokens")),
                        cached_input_tokens=_as_int(total.get("cachedInputTokens")),
                    ),
                    raw=p,
                )
            )
            return

        if method == Notifications.TURN_PLAN_UPDATED:
            steps_raw = p.get("steps") or p.get("plan") or []
            steps: list[PlanStep] = []
            if isinstance(steps_raw, list):
                for i, s in enumerate(steps_raw):
                    sd = _as_dict(s)
                    steps.append(
                        PlanStep(
                            step_id=str(sd.get("id") or i),
                            title=str(sd.get("title") or sd.get("step") or ""),
                            status=str(sd["status"]) if sd.get("status") else None,
                        )
                    )
            self._emit(
                TurnPlanUpdated(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    steps=steps,
                    raw=p,
                )
            )
            return

        if method == Notifications.TURN_DIFF_UPDATED:
            hunks_raw = p.get("hunks") or p.get("files") or []
            hunks: list[DiffHunk] = []
            if isinstance(hunks_raw, list):
                for h in hunks_raw:
                    hd = _as_dict(h)
                    hunks.append(
                        DiffHunk(
                            path=str(hd.get("path") or hd.get("file") or ""),
                            patch=hd.get("patch") or hd.get("diff"),
                        )
                    )
            self._emit(
                TurnDiffUpdated(
                    provider=PROVIDER,
                    thread_id=str(thread_id) if thread_id else None,
                    turn_id=str(turn_id) if turn_id else None,
                    hunks=hunks,
                    raw=p,
                )
            )
            return

        if method in (
            Notifications.THREAD_STARTED,
            Notifications.THREAD_STATUS_CHANGED,
        ):
            # Already handled / low value for v1 stream.
            return

        # Deferred / unknown families surface as warnings with raw.
        self._emit(
            RuntimeWarning(
                provider=PROVIDER,
                thread_id=str(thread_id) if thread_id else None,
                turn_id=str(turn_id) if turn_id else None,
                message=f"unhandled codex notification: {method}",
                code="unhandled_notification",
                raw={"method": method, "params": p},
            )
        )

    async def _handle_approval_request(self, method: str, params: Any) -> Any:
        p = _as_dict(params)
        request_id = str(p.get("requestId") or p.get("id") or uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending_approvals[request_id] = fut

        req_type: Any
        if method == ServerRequests.FILE_CHANGE_REQUEST_APPROVAL:
            req_type = "file_change"
        elif method == ServerRequests.COMMAND_EXECUTION_REQUEST_APPROVAL:
            req_type = "command_execution"
        else:
            req_type = "unknown"

        title = (
            p.get("command")
            or p.get("title")
            or p.get("reason")
            or method
        )
        self._emit(
            RequestOpened(
                provider=PROVIDER,
                thread_id=self._thread_id,
                turn_id=self._current_turn_id,
                request_id=request_id,
                request_type=req_type,
                title=str(title) if title else None,
                detail=str(p.get("detail") or p.get("command") or "") or None,
                raw={"method": method, "params": p},
            )
        )

        try:
            decision = await fut
        finally:
            self._pending_approvals.pop(request_id, None)

        native = _DECISION_MAP[decision]
        self._emit(
            RequestResolved(
                provider=PROVIDER,
                thread_id=self._thread_id,
                turn_id=self._current_turn_id,
                request_id=request_id,
                decision=native,
                raw={"decision": native},
            )
        )
        return {"decision": native}

    async def _handle_user_input_request(self, method: str, params: Any) -> Any:
        p = _as_dict(params)
        request_id = str(p.get("requestId") or p.get("id") or uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_user_input[request_id] = fut

        raw_questions = p.get("questions")
        questions: list[dict[str, Any]] = (
            [q for q in raw_questions if isinstance(q, dict)]
            if isinstance(raw_questions, list)
            else []
        )
        self._emit(
            UserInputRequested(
                provider=PROVIDER,
                thread_id=self._thread_id,
                turn_id=self._current_turn_id,
                request_id=request_id,
                prompt=str(p.get("prompt") or "") or None,
                questions=questions,
                raw={"method": method, "params": p},
            )
        )
        try:
            answers = await fut
        finally:
            self._pending_user_input.pop(request_id, None)

        self._emit(
            UserInputResolved(
                provider=PROVIDER,
                thread_id=self._thread_id,
                turn_id=self._current_turn_id,
                request_id=request_id,
                answers=answers,
            )
        )
        return {"answers": answers}


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def create_codex_harness(**kwargs: Any) -> CodexHarness:
    """Registry factory entry point."""
    return CodexHarness(**kwargs)
