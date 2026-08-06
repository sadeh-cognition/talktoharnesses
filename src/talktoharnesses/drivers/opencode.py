"""OpenCode harness — spawn ``opencode serve`` and drive HTTP + SSE."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from talktoharnesses._event_bus import EventBus
from talktoharnesses.errors import MissingDependencyError, ProcessError, SessionError
from talktoharnesses.events import (
    ContentDelta,
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
    UserInputRequested,
    UserInputResolved,
)
from talktoharnesses.transports.process import ManagedProcess, spawn_process
from talktoharnesses.transports.sse import aiter_sse_json
from talktoharnesses.types import (
    ApprovalDecision,
    Capabilities,
    SendTurnInput,
    Session,
    SessionStartInput,
)

PROVIDER = "opencode"

_REPLY_MAP: dict[str, str] = {
    "accept": "once",
    "accept_for_session": "always",
    "decline": "reject",
}


def _free_port(host: str = "127.0.0.1") -> int:
    """Reserve a port by binding it, then hand the number over.

    Inherently racy: between our close and the server's bind, something else
    can take it. ``SO_REUSEADDR`` plus the retry in ``_ensure_server`` keeps
    that from being fatal.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return int(s.getsockname()[1])


async def _wait_for_port(
    host: str,
    port: int,
    *,
    timeout: float = 30.0,
    proc: ManagedProcess | None = None,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        except OSError:
            # A server that died on startup will never open the port. Report
            # its stderr rather than making the caller wait out the timeout.
            if proc is not None and not proc.is_running():
                raise _server_start_error(host, port, proc, "exited during startup") from None
            if asyncio.get_running_loop().time() >= deadline:
                raise _server_start_error(
                    host, port, proc, f"did not accept connections within {timeout:g}s"
                ) from None
            await asyncio.sleep(0.05)


def _server_start_error(
    host: str,
    port: int,
    proc: ManagedProcess | None,
    reason: str,
) -> ProcessError:
    detail = f"OpenCode server {reason} on {host}:{port}"
    tail = proc.stderr_tail() if proc is not None else ""
    returncode = proc.returncode if proc is not None else None
    if returncode is not None:
        detail += f" (exited with code {returncode})"
    if tail:
        detail += f"\n--- opencode stderr ---\n{tail}"
    return ProcessError(detail, returncode=returncode, stderr=tail or None)


class OpenCodeHarness:
    """Harness for the local OpenCode HTTP + SSE server."""

    name = PROVIDER
    capabilities = Capabilities(
        session_model_switch="unsupported",
        interrupt_turn="in-session",
        approval="in-session",
        user_input="in-session",
        resume_session="unsupported",
    )

    def __init__(
        self,
        *,
        cwd: Path | str = ".",
        model: str | None = None,
        binary: str | None = None,
        env: Mapping[str, str] | None = None,
        hostname: str = "127.0.0.1",
        port: int | None = None,
        base_url: str | None = None,
        command: Sequence[str] | None = None,
        **_ignored: Any,
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.model = model
        self.binary = binary or "opencode"
        self.env = dict(env or {})
        self.hostname = hostname
        self._port = port
        self._base_url_override = base_url
        self._command_override = list(command) if command is not None else None

        self._proc: ManagedProcess | None = None
        self._http: Any = None
        self._bus = EventBus()
        self._session: Session | None = None
        self._session_id: str | None = None
        self._current_turn_id: str | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._pending_approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._pending_user_input: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._closed = False
        self._base_url: str | None = None

    async def start_session(self, input: SessionStartInput | None = None) -> Session:
        if self._session is not None:
            return self._session

        await self._ensure_server()
        assert self._http is not None and self._base_url is not None

        body: dict[str, Any] = {"directory": str(self.cwd)}
        if self.model:
            # model shape varies; pass as title/metadata for lightweight servers
            body["title"] = f"talktoharnesses:{self.model}"

        resp = await self._http.post(f"{self._base_url}/session", json=body)
        resp.raise_for_status()
        data = resp.json()
        session_id = str(data.get("id") or data.get("sessionID") or uuid4())
        self._session_id = session_id

        self._sse_task = asyncio.create_task(self._sse_loop(), name="opencode-sse")

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
                raw=data if isinstance(data, dict) else None,
            )
        )
        self._emit(ThreadStarted(provider=PROVIDER, thread_id=session_id))
        return self._session

    def send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        return self._send_turn(prompt)

    async def _send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        if self._session is None or self._http is None or self._session_id is None:
            raise SessionError("start_session() must be called before send_turn()")

        text = prompt if isinstance(prompt, str) else prompt.prompt
        turn_id = str(uuid4())
        self._current_turn_id = turn_id

        q = self._bus.subscribe()
        started = TurnStarted(
            provider=PROVIDER,
            thread_id=self._session_id,
            turn_id=turn_id,
        )
        self._emit(started)
        yield started

        body = {
            "parts": [{"type": "text", "text": text}],
        }
        resp = await self._http.post(
            f"{self._base_url}/session/{self._session_id}/prompt_async",
            json=body,
        )
        # 204 No Content is success for prompt_async
        if resp.status_code not in (200, 204):
            resp.raise_for_status()

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
            self._bus.unsubscribe(q)

    def stream_events(self) -> AsyncIterator[RuntimeEvent]:
        return self._stream_events()

    async def _stream_events(self) -> AsyncIterator[RuntimeEvent]:
        q = self._bus.subscribe()
        async for event in self._bus.iter_queue(q):
            yield event

    async def interrupt_turn(self, turn_id: str | None = None) -> None:
        if self._http is None or self._session_id is None:
            raise SessionError("no active session")
        await self._http.post(f"{self._base_url}/session/{self._session_id}/abort")
        self._emit(
            TurnAborted(
                provider=PROVIDER,
                thread_id=self._session_id,
                turn_id=turn_id or self._current_turn_id,
                reason="interrupted",
            )
        )

    async def respond(self, request_id: str, decision: ApprovalDecision) -> None:
        if self._http is None:
            raise SessionError("no active session")
        reply = _REPLY_MAP[decision]
        resp = await self._http.post(
            f"{self._base_url}/permission/{request_id}/reply",
            json={"reply": reply},
        )
        resp.raise_for_status()
        fut = self._pending_approvals.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        # request.resolved is emitted from the server's own permission.replied
        # event, not here: only the server can confirm the decision was applied,
        # and its SSE ordering puts the confirmation ahead of turn completion.

    async def respond_to_user_input(
        self,
        request_id: str,
        answers: Mapping[str, Any],
    ) -> None:
        if self._http is None:
            raise SessionError("no active session")
        resp = await self._http.post(
            f"{self._base_url}/question/{request_id}/reply",
            json={"answers": dict(answers)},
        )
        resp.raise_for_status()
        self._emit(
            UserInputResolved(
                provider=PROVIDER,
                thread_id=self._session_id,
                turn_id=self._current_turn_id,
                request_id=request_id,
                answers=dict(answers),
            )
        )
        fut = self._pending_user_input.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(dict(answers))

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
        self._session_id = None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.close()
        if self._sse_task is not None and not self._sse_task.done():
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sse_task
            self._sse_task = None
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None
        if self._proc is not None:
            await self._proc.aclose(timeout=5.0)
            self._proc = None

    def _emit(self, event: RuntimeEvent) -> None:
        self._bus.publish(event)

    async def _ensure_server(self) -> None:
        if self._http is not None:
            return
        try:
            import httpx
        except ImportError as exc:
            raise MissingDependencyError(PROVIDER, "httpx", extra="opencode") from exc

        if self._base_url_override:
            self._base_url = self._base_url_override.rstrip("/")
            self._http = httpx.AsyncClient(timeout=60.0)
            return

        # An explicit port is the caller's business; a picked one may have been
        # taken between reserving and binding, so give that a couple of tries.
        attempts = 1 if self._port else 3
        last_error: ProcessError | None = None
        for attempt in range(attempts):
            port = self._port or _free_port(self.hostname)
            try:
                await self._spawn_server(port)
            except ProcessError as exc:
                last_error = exc
                if attempt == attempts - 1:
                    raise
                continue
            return
        if last_error is not None:  # pragma: no cover — loop always returns or raises
            raise last_error

    async def _spawn_server(self, port: int) -> None:
        import httpx

        if self._command_override is not None:
            cmd = list(self._command_override)
            # Allow fixture to receive host/port via env
            env = {
                **self.env,
                "TALKTOHARNESSES_OPENCODE_HOST": self.hostname,
                "TALKTOHARNESSES_OPENCODE_PORT": str(port),
            }
        else:
            cmd = [
                self.binary,
                "serve",
                f"--hostname={self.hostname}",
                f"--port={port}",
            ]
            env = self.env

        self._proc = await spawn_process(cmd, cwd=self.cwd, env=env or None)
        try:
            await _wait_for_port(self.hostname, port, timeout=30.0, proc=self._proc)
        except ProcessError:
            # Tear the dead server down before the caller retries on a new port.
            await self._proc.aclose(timeout=5.0)
            self._proc = None
            raise
        self._base_url = f"http://{self.hostname}:{port}"
        self._http = httpx.AsyncClient(timeout=60.0)

    async def _sse_loop(self) -> None:
        assert self._http is not None and self._base_url is not None
        try:
            async with self._http.stream("GET", f"{self._base_url}/event") as resp:
                resp.raise_for_status()
                async for obj in aiter_sse_json(resp):
                    self._handle_event(obj)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit(
                RuntimeWarning(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    message=f"SSE stream ended: {exc}",
                    code="sse_closed",
                )
            )

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        raw_props = event.get("properties")
        props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
        raw_info = props.get("info")
        info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        session_id = props.get("sessionID") or props.get("sessionId") or info.get("id")
        # Filter to our session when possible (permission events are global).
        if (
            self._session_id
            and session_id
            and str(session_id) != self._session_id
            and etype
            not in (
                "server.connected",
                "permission.asked",
                "permission.replied",
            )
        ):
            return

        if etype == "message.part.delta":
            delta = props.get("delta") or ""
            self._emit(
                ContentDelta(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=self._current_turn_id,
                    item_id=str(props["partID"]) if props.get("partID") else None,
                    text=str(delta),
                    content_kind="text",
                    raw=event,
                )
            )
            return

        if etype == "message.part.updated":
            raw_part = props.get("part")
            part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}
            text = part.get("text")
            if text:
                self._emit(
                    ContentDelta(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=self._current_turn_id,
                        item_id=str(part.get("id") or props.get("partID") or "") or None,
                        text=str(text),
                        content_kind="text",
                        raw=event,
                    )
                )
            return

        if etype == "session.status":
            raw_status = props.get("status")
            status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
            stype = status.get("type")
            if stype == "idle" and self._current_turn_id:
                self._emit(
                    TurnCompleted(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=self._current_turn_id,
                        stop_reason="idle",
                        raw=event,
                    )
                )
            elif stype == "retry":
                self._emit(
                    RuntimeWarning(
                        provider=PROVIDER,
                        thread_id=self._session_id,
                        turn_id=self._current_turn_id,
                        message=str(status.get("message") or "retry"),
                        raw=event,
                    )
                )
            return

        if etype == "session.error":
            raw_err = props.get("error")
            err: dict[str, Any] = raw_err if isinstance(raw_err, dict) else {}
            msg = err.get("message") if err else str(raw_err)
            self._emit(
                TurnAborted(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=self._current_turn_id,
                    reason=str(msg or "session.error"),
                    raw=event,
                )
            )
            return

        if etype == "permission.asked":
            request_id = str(props.get("id") or uuid4())
            permission = str(props.get("permission") or "unknown")
            patterns = props.get("patterns") or []
            detail = "\n".join(str(p) for p in patterns) if patterns else permission
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[ApprovalDecision] = loop.create_future()
            self._pending_approvals[request_id] = fut
            self._emit(
                RequestOpened(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=self._current_turn_id,
                    request_id=request_id,
                    request_type=_map_permission(permission),
                    title=permission,
                    detail=detail,
                    raw=event,
                )
            )
            return

        if etype == "question.asked":
            request_id = str(props.get("id") or uuid4())
            loop = asyncio.get_running_loop()
            fut2: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending_user_input[request_id] = fut2
            raw_questions = props.get("questions")
            questions: list[dict[str, Any]] = (
                [q for q in raw_questions if isinstance(q, dict)]
                if isinstance(raw_questions, list)
                else []
            )
            self._emit(
                UserInputRequested(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=self._current_turn_id,
                    request_id=request_id,
                    prompt=str(props.get("message") or "") or None,
                    questions=questions,
                    raw=event,
                )
            )
            return

        if etype == "permission.replied":
            # The server confirming a decision it actually applied.
            self._emit(
                RequestResolved(
                    provider=PROVIDER,
                    thread_id=self._session_id,
                    turn_id=self._current_turn_id,
                    request_id=str(props.get("id") or ""),
                    decision=str(props.get("reply") or "unknown"),
                    raw=event,
                )
            )
            return

        if etype in (
            "session.updated",
            "session.created",
            "message.updated",
            "server.connected",
        ):
            return

        self._emit(
            RuntimeWarning(
                provider=PROVIDER,
                thread_id=self._session_id,
                turn_id=self._current_turn_id,
                message=f"unhandled opencode event: {etype}",
                code="unhandled_opencode_event",
                raw=event,
            )
        )


def _map_permission(permission: str) -> Any:
    if "edit" in permission or "write" in permission or "bash" in permission:
        if "edit" in permission or "write" in permission:
            return "file_change"
        return "command_execution"
    return "unknown"


def create_opencode_harness(**kwargs: Any) -> OpenCodeHarness:
    return OpenCodeHarness(**kwargs)
