"""ACP session lifecycle shared by Cursor and Grok drivers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from talktoharnesses._event_bus import EventBus
from talktoharnesses.acp.normalize import normalize_session_update
from talktoharnesses.errors import (
    ApprovalError,
    MissingDependencyError,
    SessionError,
)
from talktoharnesses.events import (
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
from talktoharnesses.types import (
    ApprovalDecision,
    Capabilities,
    SendTurnInput,
    Session,
    SessionStartInput,
)

# ACP permission option kinds (agent-client-protocol PermissionOption.kind)
_KIND_BY_DECISION: dict[str, str] = {
    "accept": "allow_once",
    "accept_for_session": "allow_always",
    "decline": "reject_once",
}

#: ACP has no ``session/set_model`` in the schema this package targets; agents
#: expose model choice as a *select* config option instead. Match on these.
_MODEL_CONFIG_HINTS = ("model", "modelid", "model_id")


def _is_model_option(option: Any) -> bool:
    identifier = str(_attr(option, "id") or "").lower().replace("-", "").replace("_", "")
    name = str(_attr(option, "name") or "").lower()
    return any(h.replace("_", "") in identifier for h in _MODEL_CONFIG_HINTS) or "model" in name


def _attr(obj: Any, name: str) -> Any:
    """Read a field from a pydantic model or a plain dict."""
    if isinstance(obj, Mapping):
        camel = name.split("_")[0] + "".join(p.title() for p in name.split("_")[1:])
        return obj.get(name, obj.get(camel))
    return getattr(obj, name, None)


def resolve_model_config_update(
    config_options: Any,
    model: str,
) -> tuple[str, str] | None:
    """Find ``(config_id, value)`` that selects ``model``, if the agent offers it.

    Returns ``None`` when the agent exposes no model selector, or none of its
    choices match — the caller warns rather than silently running the default.
    """
    if not isinstance(config_options, list):
        return None
    wanted = model.strip().lower()
    for option in config_options:
        if not _is_model_option(option):
            continue
        choices = _attr(option, "options")
        config_id = _attr(option, "id")
        if not isinstance(choices, list) or not config_id:
            continue
        for choice in choices:
            value = _attr(choice, "value")
            label = _attr(choice, "name")
            if value is None:
                continue
            if str(value).lower() == wanted or str(label or "").lower() == wanted:
                return str(config_id), str(value)
    return None


@dataclass
class AcpSpawnInput:
    """How to launch an ACP agent process."""

    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    provider: str = "acp"
    auth_method_id: str | None = None
    """If set, call ``authenticate`` after initialize with this method id."""
    client_info_name: str = "talktoharnesses"


class AcpRuntime:
    """Drive an ACP agent subprocess and emit canonical events."""

    capabilities = Capabilities(
        session_model_switch="unsupported",
        interrupt_turn="in-session",
        approval="in-session",
        user_input="in-session",
        resume_session="in-session",
    )

    def __init__(self, spawn: AcpSpawnInput, *, model: str | None = None) -> None:
        self.spawn = spawn
        self.model = model
        self.name = spawn.provider
        self._bus = EventBus()
        self._proc: ManagedProcess | None = None
        self._conn: Any = None
        self._client: _HarnessAcpClient | None = None
        self._session: Session | None = None
        self._session_id: str | None = None
        self._current_turn_id: str | None = None
        self._pending_approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._pending_user_input: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # ACP dispatches session/update notifications as concurrent tasks, so a
        # trailing update can still be in flight when prompt() returns. Track
        # them so turn.completed can wait for quiescence instead of guessing.
        self._updates_in_flight = 0
        self._updates_idle = asyncio.Event()
        self._updates_idle.set()
        self._closed = False

    async def start_session(self, input: SessionStartInput | None = None) -> Session:
        if self._session is not None:
            return self._session

        await self._ensure_connection()
        assert self._conn is not None

        from acp import PROTOCOL_VERSION
        from acp.schema import ClientCapabilities

        await self._conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info={"name": self.spawn.client_info_name, "version": "0.1.0"},
        )

        if self.spawn.auth_method_id:
            await self._conn.authenticate(self.spawn.auth_method_id)

        cwd = str(self.spawn.cwd or Path.cwd())
        if input and input.resume:
            resp = await self._conn.load_session(session_id=input.resume, cwd=cwd)
            session_id = str(getattr(resp, "session_id", None) or input.resume)
        else:
            resp = await self._conn.new_session(cwd=cwd)
            session_id = str(
                getattr(resp, "session_id", None)
                or getattr(resp, "sessionId", None)
                or uuid4()
            )

        self._session_id = session_id
        requested_model = (input.model if input else None) or self.model
        applied_model = await self._apply_model(session_id, resp, requested_model)
        self._session = Session(
            session_id=session_id,
            thread_id=session_id,
            provider=self.name,
            # Only report a model we actually put into effect — reporting the
            # requested one would claim a configuration that is not running.
            model=applied_model,
            started_at=datetime.now(UTC),
        )
        self._emit(
            SessionStarted(
                provider=self.name,
                session_id=session_id,
                thread_id=session_id,
                model=applied_model,
            )
        )
        self._emit(ThreadStarted(provider=self.name, thread_id=session_id))
        return self._session

    def send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        return self._send_turn(prompt)

    async def _send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        if self._session is None or self._conn is None or self._session_id is None:
            raise SessionError("start_session() must be called before send_turn()")

        from acp.schema import TextContentBlock

        text = prompt if isinstance(prompt, str) else prompt.prompt
        turn_id = str(uuid4())
        self._current_turn_id = turn_id
        if self._client is not None:
            self._client.turn_id = turn_id

        q = self._bus.subscribe()
        started = TurnStarted(
            provider=self.name,
            thread_id=self._session_id,
            turn_id=turn_id,
        )
        self._emit(started)
        yield started

        # Fire prompt; session_update notifications arrive concurrently.
        # When the prompt RPC returns, synthesize turn.completed if needed.
        async def _run_prompt() -> None:
            try:
                resp = await self._conn.prompt(
                    session_id=self._session_id,
                    prompt=[TextContentBlock(type="text", text=text)],
                )
                stop = getattr(resp, "stop_reason", None) or getattr(
                    resp, "stopReason", None
                )
                # Let trailing session/update notifications land on the bus
                # before turn.completed closes the send_turn view.
                await self._drain_session_updates()
                self._emit(
                    TurnCompleted(
                        provider=self.name,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        stop_reason=str(stop) if stop else "end_turn",
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    TurnAborted(
                        provider=self.name,
                        thread_id=self._session_id,
                        turn_id=turn_id,
                        reason=str(exc),
                    )
                )

        prompt_task = asyncio.create_task(_run_prompt())

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
            if not prompt_task.done():
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt_task
            self._bus.unsubscribe(q)

    def stream_events(self) -> AsyncIterator[RuntimeEvent]:
        return self._stream_events()

    async def _stream_events(self) -> AsyncIterator[RuntimeEvent]:
        q = self._bus.subscribe()
        async for event in self._bus.iter_queue(q):
            yield event

    async def interrupt_turn(self, turn_id: str | None = None) -> None:
        if self._conn is None or self._session_id is None:
            raise SessionError("no active session")
        await self._conn.cancel(session_id=self._session_id)
        self._emit(
            TurnAborted(
                provider=self.name,
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
        fut = self._pending_user_input.get(request_id)
        if fut is None or fut.done():
            raise ApprovalError(f"no open user-input request {request_id!r}")
        fut.set_result(dict(answers))

    async def stop_session(self) -> None:
        if self._conn is not None and self._session_id is not None:
            with contextlib.suppress(Exception):
                await self._conn.close_session(self._session_id)
        if self._session is not None:
            self._emit(
                SessionExited(
                    provider=self.name,
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
        pending: list[asyncio.Future[Any]] = [
            *self._pending_approvals.values(),
            *self._pending_user_input.values(),
        ]
        for fut in pending:
            if not fut.done():
                fut.cancel()
        self._pending_approvals.clear()
        self._pending_user_input.clear()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
        if self._proc is not None:
            await self._proc.aclose(timeout=5.0)
            self._proc = None

    def _emit(self, event: RuntimeEvent) -> None:
        self._bus.publish(event)

    async def _drain_session_updates(self, *, timeout: float = 2.0) -> None:
        """Wait until no session/update handler is still running.

        The ACP dispatcher spawns a task per notification, so an update queued
        just before the prompt response may not have run yet. Yielding once
        lets every already-scheduled task start; the event then covers handlers
        that await internally. Bounded, so a wedged handler cannot hang a turn.
        """
        await asyncio.sleep(0)
        if self._updates_idle.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._updates_idle.wait(), timeout=timeout)

    def _update_started(self) -> None:
        self._updates_in_flight += 1
        self._updates_idle.clear()

    def _update_finished(self) -> None:
        self._updates_in_flight = max(0, self._updates_in_flight - 1)
        if self._updates_in_flight == 0:
            self._updates_idle.set()

    async def _apply_model(
        self,
        session_id: str,
        session_response: Any,
        model: str | None,
    ) -> str | None:
        """Select ``model`` through the agent's config options.

        Returns the model actually applied, or ``None``. A request we cannot
        honour produces a ``runtime.warning`` rather than being dropped —
        otherwise the caller has no way to tell that ``model=`` did nothing.
        """
        if not model:
            return None

        config_options = getattr(session_response, "config_options", None)
        update = resolve_model_config_update(config_options, model)
        if update is None:
            self._emit(
                RuntimeWarning(
                    provider=self.name,
                    thread_id=session_id,
                    message=(
                        f"{self.name} did not offer a model option matching {model!r}; "
                        "the agent's own default is in effect"
                    ),
                    code="model_not_applied",
                    raw={"requested_model": model},
                )
            )
            return None

        config_id, value = update
        try:
            assert self._conn is not None
            await self._conn.set_config_option(
                config_id=config_id, session_id=session_id, value=value
            )
        except Exception as exc:  # noqa: BLE001 — a rejected model is not fatal
            self._emit(
                RuntimeWarning(
                    provider=self.name,
                    thread_id=session_id,
                    message=f"failed to select model {model!r}: {exc}",
                    code="model_not_applied",
                    raw={"requested_model": model, "config_id": config_id},
                )
            )
            return None
        return value

    async def _ensure_connection(self) -> None:
        if self._conn is not None:
            return
        try:
            from acp import connect_to_agent
        except ImportError as exc:
            raise MissingDependencyError(
                self.name, "agent-client-protocol", extra="acp"
            ) from exc

        self._proc = await spawn_process(
            self.spawn.command,
            cwd=self.spawn.cwd,
            env=self.spawn.env or None,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None

        self._client = _HarnessAcpClient(self)
        # connect_to_agent(client, writer, reader)
        # Cast: _HarnessAcpClient structurally implements acp.Client.
        from acp.interfaces import Client as AcpClient

        self._conn = connect_to_agent(
            cast(AcpClient, self._client),
            self._proc.stdin,
            self._proc.stdout,
        )


class _HarnessAcpClient:
    """Implements the ACP Client surface the agent calls back into."""

    def __init__(self, runtime: AcpRuntime) -> None:
        self._rt = runtime
        self.turn_id: str | None = None

    def on_connect(self, conn: Any) -> None:
        return None

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._rt._update_started()
        try:
            events = normalize_session_update(
                provider=self._rt.name,
                session_id=session_id,
                thread_id=self._rt._session_id,
                turn_id=self.turn_id or self._rt._current_turn_id,
                update=update,
            )
            for ev in events:
                self._rt._emit(ev)
        finally:
            self._rt._update_finished()

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any],
        **kwargs: Any,
    ) -> Any:
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        request_id = str(uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._rt._pending_approvals[request_id] = fut

        tool_name = None
        title = None
        if tool_call is not None:
            tool_name = getattr(tool_call, "title", None) or getattr(tool_call, "kind", None)
            if isinstance(tool_call, dict):
                tool_name = tool_call.get("title") or tool_call.get("kind")
            title = str(tool_name) if tool_name else None

        self._rt._emit(
            RequestOpened(
                provider=self._rt.name,
                thread_id=session_id,
                turn_id=self.turn_id or self._rt._current_turn_id,
                request_id=request_id,
                request_type="dynamic_tool_call",
                title=title,
                tool_name=str(tool_name) if tool_name else None,
                raw={
                    "tool_call": _maybe_dump(tool_call),
                    "options": [_maybe_dump(o) for o in options],
                },
            )
        )

        try:
            decision = await fut
        finally:
            self._rt._pending_approvals.pop(request_id, None)

        kind = _KIND_BY_DECISION[decision]
        option_id = _match_option_id(options, kind)

        if decision == "decline" and option_id is None:
            outcome: Any = DeniedOutcome(outcome="cancelled")
            native = "cancelled"
        else:
            # Prefer matching option; fall back to synthetic id of the kind.
            oid = option_id or kind
            outcome = AllowedOutcome(outcome="selected", option_id=oid)
            native = kind

        self._rt._emit(
            RequestResolved(
                provider=self._rt.name,
                thread_id=session_id,
                turn_id=self.turn_id or self._rt._current_turn_id,
                request_id=request_id,
                decision=native,
                raw={"outcome": _maybe_dump(outcome)},
            )
        )
        return RequestPermissionResponse(outcome=outcome)

    async def create_elicitation(
        self, message: str, mode: Any, **kwargs: Any
    ) -> Any:
        from acp.schema import DeclineElicitationResponse

        request_id = str(uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._rt._pending_user_input[request_id] = fut
        self._rt._emit(
            UserInputRequested(
                provider=self._rt.name,
                thread_id=self._rt._session_id,
                turn_id=self.turn_id or self._rt._current_turn_id,
                request_id=request_id,
                prompt=message,
                raw={"mode": str(mode), **kwargs},
            )
        )
        try:
            answers = await fut
        finally:
            self._rt._pending_user_input.pop(request_id, None)

        self._rt._emit(
            UserInputResolved(
                provider=self._rt.name,
                thread_id=self._rt._session_id,
                turn_id=self.turn_id or self._rt._current_turn_id,
                request_id=request_id,
                answers=answers,
            )
        )
        if "_response" in answers:
            return answers["_response"]
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    def _resolve_within_workspace(self, path: str) -> Path:
        """Resolve ``path``, refusing anything outside the session workspace.

        The agent chooses these paths. Serving arbitrary absolute paths would
        let a prompt-injected agent read ``~/.ssh/id_rsa`` or ``.env`` through a
        harness the caller only pointed at one project.
        """
        root = (self._rt.spawn.cwd or Path.cwd()).resolve()
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionError(f"path {path!r} is outside the workspace {str(root)!r}")
        return resolved

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> None:
        # Previously a silent no-op, which told the agent the write had
        # succeeded and let it build on a file that was never created.
        target = self._resolve_within_workspace(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.schema import ReadTextFileResponse

        source = self._resolve_within_workspace(path)
        # Let OSError surface: the peer maps it to a JSON-RPC error, which is
        # what "missing file" should look like. Returning "" made an absent
        # file indistinguishable from an empty one.
        text = source.read_text(encoding="utf-8")
        if line is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = max(0, (line or 1) - 1)
            end = start + limit if limit is not None else None
            text = "".join(lines[start:end])
        return ReadTextFileResponse(content=text)

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError("terminal not supported in talktoharnesses v1")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        raise NotImplementedError("terminal not supported in talktoharnesses v1")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        return None

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> Any:
        raise NotImplementedError("terminal not supported in talktoharnesses v1")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


def _maybe_dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, mode="json")
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    return repr(obj)


def _match_option_id(options: Sequence[Any], kind: str) -> str | None:
    for opt in options:
        opt_kind = getattr(opt, "kind", None)
        opt_id = getattr(opt, "option_id", None) or getattr(opt, "optionId", None)
        if isinstance(opt, dict):
            opt_kind = opt.get("kind")
            opt_id = opt.get("optionId") or opt.get("option_id")
        if opt_kind == kind and opt_id is not None:
            return str(opt_id)
    return None
