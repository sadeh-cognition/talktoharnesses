"""Prime Agent HarnessAdapter over its JSONL RPC mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any, Literal, cast
from uuid import uuid4

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import HarnessEvent
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
)
from talktoharnesses.providers.acp.framing import FrameDecodeError, iter_json_frames
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.prime_agent.argv import build_prime_agent_argv
from talktoharnesses.providers.prime_agent.compatibility import (
    PrimeAgentReleaseRecord,
    enforce_published_operation,
)
from talktoharnesses.providers.prime_agent.normalizer import PrimeAgentNormalizer
from talktoharnesses.providers.prime_agent.probe import probe_prime_agent
from talktoharnesses.runtime.handle import ProcessHandle


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in cast(dict[object, object], value).items()}


class PrimeAgentAdapter:
    """One supervised Prime Agent RPC client per conversation runtime."""

    kind: HarnessKind = HarnessKind.PRIME_AGENT

    def __init__(self) -> None:
        self._process: ProcessHandle | None = None
        self._release: PrimeAgentReleaseRecord | None = None
        self._normalizer = PrimeAgentNormalizer()
        self._session: HarnessSession | None = None
        self._current_model: str | None = None
        self._event_q: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._router_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False

    def bind_process(self, process: ProcessHandle) -> None:
        self._process = process

    def set_redaction_patterns(self, patterns: tuple[str, ...]) -> None:
        self._normalizer.set_redaction_patterns(patterns)

    def build_argv(self, config: HarnessConfiguration) -> tuple[str, ...]:
        return build_prime_agent_argv(model=config.model, thinking=config.mode)

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        capabilities, release = await probe_prime_agent(config)
        self._release = release
        return capabilities

    def preflight_operation(self, mode: Literal["create", "resume"]) -> None:
        if self._release is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "prime agent adapter must be probed before operation",
            )
        enforce_published_operation(self._release, mode=mode)

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        self.preflight_operation("create")
        state = await self._request("get_state")
        native_session_id = self._session_file(state)
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.PRIME_AGENT,
            native_session_id=native_session_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
            metadata={"session_id": str(state.get("sessionId") or "")},
        )
        self._session = session
        self._current_model = session.model
        return session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        self.preflight_operation("resume")
        await self._request("switch_session", sessionPath=request.native_session_id)
        state = await self._request("get_state")
        native_session_id = self._session_file(state)
        if native_session_id != request.native_session_id:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "prime agent resumed a different session",
                details={"expected": request.native_session_id, "got": native_session_id},
            )
        session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.PRIME_AGENT,
            native_session_id=native_session_id,
            model=request.configuration.model,
            mode=request.configuration.mode,
            metadata={"session_id": str(state.get("sessionId") or "")},
        )
        self._session = session
        self._current_model = session.model
        return session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        self._require_session(session)
        desired_model = request.model or session.model
        if desired_model and desired_model != self._current_model:
            provider, model_id = self._split_model(desired_model)
            await self._request("set_model", provider=provider, modelId=model_id)
            self._current_model = desired_model
        self._normalizer.begin_turn(request.turn_id)
        try:
            await self._request("prompt", message=request.prompt)
        except Exception:
            for event in self._normalizer.on_outcome_unknown("prime agent rejected the prompt"):
                await self._event_q.put(event)
            raise

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        self._require_session(session)
        if not self._normalizer.turn_active:
            return False
        await self._request("steer", message=request.prompt)
        return True

    async def interrupt(self, session: HarnessSession) -> None:
        self._require_session(session)
        if self._normalizer.turn_active:
            await self._request("abort")

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        self._require_session(session)
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "prime agent has no pending interaction",
            details={"interaction_id": str(answer.interaction_id)},
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
        del session
        if self._closed:
            return
        self._closed = True
        if self._process is not None:
            close_stdin = getattr(self._process, "close_stdin", None)
            if callable(close_stdin):
                result = close_stdin()
                if asyncio.iscoroutine(result):
                    await result
        if self._router_task is not None and self._router_task is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await self._router_task
        self._fail_pending("prime agent adapter closed")
        with contextlib.suppress(asyncio.QueueFull):
            self._event_q.put_nowait(None)

    async def _request(self, command: str, **params: object) -> dict[str, Any]:
        await self._ensure_router()
        assert self._process is not None
        request_id = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = json.dumps({"id": request_id, "type": command, **params}, separators=(",", ":"))
        try:
            async with self._write_lock:
                await self._process.write_stdin((frame + "\n").encode("utf-8"))
            response = await future
        finally:
            self._pending.pop(request_id, None)
        if response.get("success") is not True:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                f"prime agent {command} failed: {response.get('error') or 'unknown error'}",
            )
        return _mapping(response.get("data"))

    async def _ensure_router(self) -> None:
        if self._process is None:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "PrimeAgentAdapter has no bound process; RuntimeManager must bind_process first",
            )
        if self._router_task is None:
            self._router_task = asyncio.create_task(self._router_loop(), name="prime-agent-rpc")

    async def _router_loop(self) -> None:
        assert self._process is not None
        error_message: str | None = None
        try:
            async for frame in iter_json_frames(self._process.stdout()):
                if frame.get("type") == "response":
                    request_id = frame.get("id")
                    pending = self._pending.get(str(request_id))
                    if pending is None:
                        raise DomainError(
                            ErrorCode.PROTOCOL_ERROR,
                            "prime agent response has unknown request id",
                        )
                    if not pending.done():
                        pending.set_result(frame)
                    continue
                if frame.get("type") == "extension_ui_request":
                    await self._dismiss_extension_ui(frame)
                    continue
                for event in self._normalizer.on_event(frame):
                    await self._event_q.put(event)
        except asyncio.CancelledError:
            raise
        except (FrameDecodeError, DomainError) as exc:
            error_message = str(exc)
        except Exception as exc:  # noqa: BLE001
            error_message = str(exc)
        finally:
            if error_message is None and not self._closed:
                error_message = "prime agent RPC stream closed"
            if error_message is not None:
                self._fail_pending(error_message)
                for event in self._normalizer.on_outcome_unknown(error_message):
                    await self._event_q.put(event)
            if not self._closed:
                await self._event_q.put(None)

    def _fail_pending(self, message: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DomainError(ErrorCode.PROTOCOL_ERROR, message))

    async def _dismiss_extension_ui(self, request: dict[str, Any]) -> None:
        """Cancel unsupported dialogs; fire-and-forget UI requests need no reply."""
        if request.get("method") not in {"select", "confirm", "input", "editor"}:
            return
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "prime agent extension UI request missing id",
            )
        assert self._process is not None
        frame = json.dumps(
            {"type": "extension_ui_response", "id": request_id, "cancelled": True},
            separators=(",", ":"),
        )
        async with self._write_lock:
            await self._process.write_stdin((frame + "\n").encode("utf-8"))

    def _require_session(self, session: HarnessSession) -> None:
        if self._session is None or session != self._session:
            raise DomainError(ErrorCode.INVALID_STATE, "prime agent session is not active")

    @staticmethod
    def _session_file(state: dict[str, Any]) -> str:
        session_file = state.get("sessionFile")
        if not isinstance(session_file, str) or not session_file:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "prime agent state missing persisted sessionFile",
            )
        return session_file

    @staticmethod
    def _split_model(model: str) -> tuple[str, str]:
        if "/" not in model:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "prime agent per-turn model must be provider/model",
                details={"model": model},
            )
        provider, model_id = model.split("/", 1)
        if not provider or not model_id:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "prime agent per-turn model must be provider/model",
                details={"model": model},
            )
        return provider, model_id
