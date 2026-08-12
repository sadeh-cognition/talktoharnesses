"""Cursor HarnessAdapter — ACP stdio over a RuntimeManager-bound process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from talktoharnesses import __version__
from talktoharnesses.domain.enums import ApprovalDecision, ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import HarnessEvent, InteractionRequestedPayload
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
)
from talktoharnesses.providers.acp.connection import AcpConnection
from talktoharnesses.providers.acp.jsonrpc import JsonRpcRemoteError, ProtocolCloseError
from talktoharnesses.providers.acp.protocol import (
    CURSOR_ALLOWED_OUTBOUND_METHODS,
    cursor_acp_protocol,
)
from talktoharnesses.providers.acp.schemas.cursor_ext import (
    CURSOR_CONTROL_NOTIFICATIONS,
    CursorSelectConfigOption,
    parse_cursor_config_options,
)
from talktoharnesses.providers.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from talktoharnesses.providers.cursor.argv import build_cursor_argv
from talktoharnesses.providers.cursor.compatibility import (
    CursorReleaseRecord,
    enforce_published_operation,
)
from talktoharnesses.providers.cursor.normalizer import CursorNormalizer
from talktoharnesses.providers.cursor.probe import probe_cursor
from talktoharnesses.runtime.handle import ProcessHandle

logger = logging.getLogger(__name__)

CLIENT_INFO = {"name": "talktoharnesses", "version": __version__}

_MODEL_PARAMETER_CATEGORIES: frozenset[str] = frozenset({"model_config", "thought_level"})


def _map_dict(value: object) -> dict[str, Any]:
    # Accept partially-unknown JSON dicts under strict Pyright.
    if not isinstance(value, dict):
        return {}
    raw = cast(dict[object, object], cast(object, value))
    return {str(k): v for k, v in raw.items()}


@dataclass(frozen=True, slots=True)
class _CursorModelSelection:
    model_id: str
    parameters: tuple[tuple[str, str], ...] = ()


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
        self._config_options: tuple[CursorSelectConfigOption, ...] = ()
        self._session_model_selection: _CursorModelSelection | None = None
        self._current_model_selection: _CursorModelSelection | None = None
        self._force = False

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
        return build_cursor_argv()

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        caps, release = await probe_cursor(config)
        self._capabilities = caps
        self._release = release
        return caps

    def preflight_operation(self, mode: Literal["create", "resume"]) -> None:
        if self._release is None:
            raise DomainError(
                ErrorCode.INVALID_STATE, "cursor adapter must be probed before operation"
            )
        enforce_published_operation(self._release, mode=mode)

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        self._force = request.configuration.force
        self.preflight_operation("create")
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
        await self._apply_session_configuration(
            session_id=session_id,
            session_result=result,
            configured_model=request.configuration.model,
            configured_mode=request.configuration.mode,
        )
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
        self._force = request.configuration.force
        self.preflight_operation("resume")
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
        session_id = _session_id_or_none(result) or request.native_session_id
        if not session_id:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "session result missing sessionId")
        await self._apply_session_configuration(
            session_id=session_id,
            session_result=result,
            configured_model=request.configuration.model,
            configured_mode=request.configuration.mode,
        )
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
        await self._apply_turn_model_selection(
            session_id=session.native_session_id,
            request_model=request.model,
        )
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
        enforce_published_operation(self._release, mode="steer")
        return False

    async def interrupt(self, session: HarnessSession) -> None:
        self._require_session(session)
        if self._release is not None and self._release.capabilities.supports_interrupt:
            enforce_published_operation(self._release, mode="interrupt")
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
                # Only the parameterized model picker; no fs/terminal reverse handlers.
                "clientCapabilities": {
                    "_meta": {
                        "parameterizedModelPicker": True,
                    }
                },
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
        # Cursor may omit agentInfo; probe already matched the pinned CLI release.
        if agent_info and (
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
        missing_methods = (
            set(self._release.required_agent_methods) - CURSOR_ALLOWED_OUTBOUND_METHODS
        )
        if missing_methods:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "adapter does not implement required agent methods",
                details={"missing_methods": sorted(missing_methods)},
            )

    async def _apply_session_configuration(
        self,
        *,
        session_id: str,
        session_result: object,
        configured_model: str | None,
        configured_mode: str | None,
    ) -> None:
        options = parse_cursor_config_options(session_result)
        model_opt = _find_config_option(options, "model")
        mode_opt = _find_config_option(options, "mode")
        if model_opt is None or mode_opt is None:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor session did not advertise required model and mode configuration options",
                details={
                    "has_model": model_opt is not None,
                    "has_mode": mode_opt is not None,
                },
            )

        if configured_model is not None:
            selection = _parse_model_selector(configured_model)
            options = await self._apply_model_selection(
                session_id=session_id,
                selection=selection,
                options=options,
                complete=False,
            )

        if configured_mode is not None:
            options = await self._set_config_option(
                session_id=session_id,
                config_id="mode",
                value=configured_mode,
                options=options,
            )

        captured = self._record_config_options(options)
        self._session_model_selection = captured

    async def _apply_turn_model_selection(
        self,
        *,
        session_id: str,
        request_model: str | None,
    ) -> None:
        if request_model is not None:
            selection = _parse_model_selector(request_model)
            options = await self._apply_model_selection(
                session_id=session_id,
                selection=selection,
                options=self._config_options,
                complete=False,
            )
            self._record_config_options(options)
            return

        baseline = self._session_model_selection
        if baseline is None:
            return
        if self._current_model_selection == baseline:
            return
        options = await self._apply_model_selection(
            session_id=session_id,
            selection=baseline,
            options=self._config_options,
            complete=True,
        )
        self._record_config_options(options)

    def _record_config_options(
        self,
        options: tuple[CursorSelectConfigOption, ...],
    ) -> _CursorModelSelection:
        current = _capture_model_selection(options)
        self._config_options = options
        self._current_model_selection = current
        return current

    async def _apply_model_selection(
        self,
        *,
        session_id: str,
        selection: _CursorModelSelection,
        options: tuple[CursorSelectConfigOption, ...],
        complete: bool,
    ) -> tuple[CursorSelectConfigOption, ...]:
        """Apply a parsed model selection.

        When ``complete`` is True, ``selection.parameters`` is the full desired
        parameter set (session baseline restore). When False, only explicit
        selector parameters are set; other parameters keep Cursor defaults after
        the model change.
        """
        if complete:
            current = _capture_model_selection(options)
            if current == selection:
                return options

        model_opt = _find_config_option(options, "model")
        if model_opt is None:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor session has no model configuration option",
            )
        if model_opt.currentValue != selection.model_id:
            options = await self._set_config_option(
                session_id=session_id,
                config_id="model",
                value=selection.model_id,
                options=options,
            )
        elif not complete and not selection.parameters:
            # Model already active and no explicit parameters — nothing to set.
            return options

        for param_id, value in selection.parameters:
            option = _find_config_option(options, param_id)
            if option is None:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "Cursor model parameter is not advertised for the selected model",
                    details={"parameter_id": param_id, "model_id": selection.model_id},
                )
            if option.category not in _MODEL_PARAMETER_CATEGORIES:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "configuration option is not a model parameter",
                    details={
                        "parameter_id": param_id,
                        "category": option.category,
                    },
                )
            if option.currentValue == value:
                continue
            options = await self._set_config_option(
                session_id=session_id,
                config_id=param_id,
                value=value,
                options=options,
            )
        return options

    async def _set_config_option(
        self,
        *,
        session_id: str,
        config_id: str,
        value: str,
        options: tuple[CursorSelectConfigOption, ...],
    ) -> tuple[CursorSelectConfigOption, ...]:
        option = _find_config_option(options, config_id)
        if option is None:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor configuration option is not advertised",
                details={"config_id": config_id},
            )
        advertised = tuple(item.value for item in option.options)
        if value not in advertised:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor configuration value is not advertised",
                details={
                    "config_id": config_id,
                    "value": value,
                    "advertised_values": list(advertised),
                },
            )
        if option.currentValue == value:
            return options

        assert self._connection is not None
        try:
            future, _ = await self._connection.request(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": config_id,
                    "value": value,
                },
            )
            result = await future
        except JsonRpcRemoteError as exc:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor rejected configuration option",
                details={"config_id": config_id, "remote_code": exc.code},
            ) from exc

        try:
            new_options = parse_cursor_config_options(result)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "malformed Cursor set_config_option response",
                details={"config_id": config_id},
            ) from exc

        updated = _find_config_option(new_options, config_id)
        if updated is None or updated.currentValue != value:
            raise DomainError(
                ErrorCode.PROTOCOL_ERROR,
                "Cursor set_config_option response did not reflect requested value",
                details={
                    "config_id": config_id,
                    "requested": value,
                    "currentValue": None if updated is None else updated.currentValue,
                },
            )
        self._record_config_options(new_options)
        return new_options

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
        if self._force:
            assert self._connection is not None
            result = self._normalizer.map_approval_decision(
                ApprovalDecision.ALLOW_SESSION,
                options,
            )
            if _map_dict(result.get("outcome")).get("outcome") == "cancelled":
                result = self._normalizer.map_approval_decision(
                    ApprovalDecision.ALLOW_ONCE,
                    options,
                )
            if _map_dict(result.get("outcome")).get("outcome") == "cancelled":
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "force permission request has no allow option",
                )
            await self._connection.respond(request.id, result)
            return None
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


def _parse_model_selector(value: str) -> _CursorModelSelection:
    selector = value.strip()
    if not selector:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor model selector is empty",
        )

    if "[" not in selector:
        model_id = selector
        if not model_id:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Cursor model selector is missing a model ID",
            )
        if model_id == "auto":
            model_id = "default"
        return _CursorModelSelection(model_id=model_id)

    if selector.count("[") != 1:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor model selector may contain at most one parameter section",
            details={"selector": selector},
        )
    open_idx = selector.index("[")
    if selector.count("]") != 1 or not selector.endswith("]"):
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor model selector must contain exactly one closing bracket",
            details={"selector": selector},
        )
    # After strip, ] must be the final character (no trailing content).
    model_id = selector[:open_idx].strip()
    if not model_id:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor model selector is missing a model ID",
            details={"selector": selector},
        )
    params_body = selector[open_idx + 1 : -1]
    parameters: list[tuple[str, str]] = []
    seen: set[str] = set()
    if params_body.strip():
        for raw_entry in params_body.split(","):
            entry = raw_entry.strip()
            if not entry:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "Cursor model selector has an empty parameter entry",
                    details={"selector": selector},
                )
            if entry.count("=") != 1:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "Cursor model selector parameter must contain exactly one '='",
                    details={"selector": selector, "entry": entry},
                )
            raw_id, raw_value = entry.split("=", 1)
            param_id = raw_id.strip()
            param_value = raw_value.strip()
            if not param_id or not param_value:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "Cursor model selector parameter ID and value must be non-empty",
                    details={"selector": selector},
                )
            if param_id in seen:
                raise DomainError(
                    ErrorCode.PROVIDER_INCOMPATIBLE,
                    "Cursor model selector has a duplicate parameter ID",
                    details={"selector": selector, "parameter_id": param_id},
                )
            seen.add(param_id)
            parameters.append((param_id, param_value))

    if model_id == "auto":
        model_id = "default"
    return _CursorModelSelection(model_id=model_id, parameters=tuple(parameters))


def _find_config_option(
    options: tuple[CursorSelectConfigOption, ...],
    config_id: str,
) -> CursorSelectConfigOption | None:
    for option in options:
        if option.id == config_id:
            return option
    return None


def _capture_model_selection(
    options: tuple[CursorSelectConfigOption, ...],
) -> _CursorModelSelection:
    model_opt = _find_config_option(options, "model")
    if model_opt is None:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "Cursor session has no model configuration option",
        )
    parameters: list[tuple[str, str]] = []
    for option in options:
        if option.category in _MODEL_PARAMETER_CATEGORIES:
            parameters.append((option.id, option.currentValue))
    return _CursorModelSelection(model_id=model_opt.currentValue, parameters=tuple(parameters))


def _session_id_or_none(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    result_map = _map_dict(cast(object, result))
    session_id = result_map.get("sessionId") or result_map.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    return None


def _require_session_id(result: Any) -> str:
    session_id = _session_id_or_none(result)
    if session_id is None:
        raise DomainError(ErrorCode.PROTOCOL_ERROR, "session result missing sessionId")
    return session_id
