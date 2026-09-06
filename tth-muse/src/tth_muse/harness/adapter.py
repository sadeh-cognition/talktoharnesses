"""Muse Code HarnessAdapter using the official MSP command and event planes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from tth_types.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from tth_types.enums import ApprovalDecision, ErrorCode, HarnessKind, InteractionKind
from tth_types.errors import DomainError
from tth_types.events import HarnessEvent, InteractionRequestedPayload
from tth_types.harness import (
    ApprovalRequestPayload,
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
    StructuredQuestionPayload,
)

from tth_muse.harness.config_dir import remove_config_dir, render_config_dir
from tth_muse.harness.connection import MuseConnection
from tth_muse.harness.normalizer import MuseNormalizer
from tth_muse.harness.probe import build_argv, probe_muse
from tth_muse.runtime.handle import ProcessHandle
from tth_muse.shared.questions import canonical_answer_values, canonical_questions

_DECISIONS = {
    "approved": ApprovalDecision.ALLOW_ONCE,
    "approvedForSession": ApprovalDecision.ALLOW_SESSION,
    "denied": ApprovalDecision.DENY,
    "abort": ApprovalDecision.CANCEL,
}


def _dict(value: object) -> dict[str, Any]:
    """MSP serializes absent optionals as ``null``; treat those as empty."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


class MuseAdapter:
    kind = HarnessKind.MUSE

    def __init__(self) -> None:
        self._process: ProcessHandle | None = None
        self._connection: MuseConnection | None = None
        self._session: HarnessSession | None = None
        self._capabilities: HarnessCapabilities | None = None
        self._normalizer = MuseNormalizer()
        self._queue: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._pending: dict[
            UUID, tuple[dict[str, Any], ApprovalRequestPayload | StructuredQuestionPayload]
        ] = {}
        self._interaction_keys: set[str] = set()
        self._answer_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._model: str | None = None
        self._queued = False
        self._closed = False
        self._config_dir: Path | None = None

    def bind_process(self, process: ProcessHandle) -> None:
        self._process = process

    def build_argv(self, config: HarnessConfiguration) -> tuple[str, ...]:
        return build_argv(config)

    def build_environment(self, config: HarnessConfiguration) -> dict[str, str]:
        """Point the host at a per-process config dir carrying the MCP servers.

        Muse reads MCP servers from its settings file, not from the wire, so a
        harness with servers gets its own ``XDG_CONFIG_HOME`` whose settings
        merge the servers over the host's saved settings. Without servers the
        host uses its ordinary configuration.
        """
        if not config.mcp_servers:
            return {}
        self._config_dir = render_config_dir(config)
        return {"XDG_CONFIG_HOME": str(self._config_dir)}

    def set_redaction_patterns(self, patterns: tuple[str, ...]) -> None:
        self._normalizer.patterns = tuple(sorted(filter(None, patterns), key=len, reverse=True))

    def import_seen(self, native_ids: frozenset[str], offsets: frozenset[str]) -> None:
        self._normalizer.import_seen(native_ids, offsets)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return self._normalizer.export_seen()

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        self._capabilities = await probe_muse(config)
        return self._capabilities

    async def _connect(self) -> MuseConnection:
        if self._capabilities is None or self._process is None:
            raise DomainError(ErrorCode.INVALID_STATE, "Muse must be probed and bound before start")
        if self._connection is None:
            self._connection = MuseConnection(
                self._process, self._notification, self._disconnected, self._normalizer.redact
            )
            await self._connection.initialize()
        return self._connection

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        connection = await self._connect()
        params: dict[str, Any] = {
            "workspaceRoot": request.launch.working_directory,
            "approvalMode": "allowAll" if request.configuration.yolo else "onRequest",
        }
        if request.configuration.model is not None:
            params["modelId"] = request.configuration.model
        result = await connection.command("session/start", **params)
        return self._record_session(request, result)

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        connection = await self._connect()
        result = await connection.command(
            "session/resume", sessionId=request.native_session_id, excludeItems=True
        )
        session = self._record_session(request, result)
        if session.native_session_id != request.native_session_id:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "Muse resumed a different session")
        await connection.command(
            "session/setApprovalMode",
            sessionId=session.native_session_id,
            mode="allowAll" if request.configuration.yolo else "onRequest",
        )
        if request.configuration.model:
            await self._set_model(request.configuration.model)
        await self._release_orphaned_requests(connection, request.native_session_id)
        return session

    async def _release_orphaned_requests(
        self, connection: MuseConnection, native_session_id: str
    ) -> None:
        """Unblock host turns still waiting on approvals nobody can answer.

        The proxy terminalizes any in-flight turn before it resumes a session,
        so requests the host still lists as pending belong to a turn that no
        longer exists on the TTH side; there is no turn to re-issue them under.
        Interrupting their native turn settles them host-side instead of
        leaving the session wedged behind an unanswerable prompt.
        """
        pending = await connection.request("approval/listPending", sessionId=native_session_id)
        orphaned: set[str] = set()
        for rows in (pending.get("approvals"), pending.get("userInputs")):
            for row in _list(rows):
                native_turn_id = _dict(row).get("turnId")
                if isinstance(native_turn_id, str) and native_turn_id:
                    orphaned.add(native_turn_id)
        for native_turn_id in sorted(orphaned):
            await connection.command(
                "turn/interrupt", sessionId=native_session_id, turnId=native_turn_id
            )

    def _record_session(
        self, request: StartSessionRequest | ResumeSessionRequest, result: dict[str, Any]
    ) -> HarnessSession:
        native = result["session"]
        if not native.get("sessionId"):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, "Muse session response has no sessionId")
        if native.get("workspaceRoot") != request.launch.working_directory:
            raise DomainError(
                ErrorCode.PROVIDER_INCOMPATIBLE,
                "Muse session workspace differs from the configured directory",
            )
        self._model = native.get("modelId")
        self._session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=self.kind,
            native_session_id=native["sessionId"],
            model=request.configuration.model,
        )
        self._normalizer.session_id = native["sessionId"]
        return self._session

    def _require(self, session: HarnessSession) -> MuseConnection:
        if self._session is None or self._closed or session.binding_id != self._session.binding_id:
            raise DomainError(ErrorCode.INVALID_STATE, "Muse session is not active")
        assert self._connection is not None
        return self._connection

    async def _set_model(self, model: str) -> None:
        assert self._connection is not None and self._session is not None
        if self._model != model:
            await self._connection.command(
                "session/setModel",
                sessionId=self._session.native_session_id,
                model={"modelId": model},
            )
            self._model = model

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        connection = self._require(session)
        if self._normalizer.turn_id == request.turn_id:
            return
        if self._normalizer.turn_id is not None:
            raise DomainError(ErrorCode.CONVERSATION_BUSY, "Muse already has an active turn")
        model = request.model or session.model
        if model:
            await self._set_model(model)
        self._normalizer.begin_turn(request.turn_id)
        native_turn_id = connection.mint_command_id()
        self._normalizer.native_turn_id = native_turn_id
        try:
            result = await connection.command(
                "turn/start",
                commandId=native_turn_id,
                sessionId=session.native_session_id,
                input=[{"type": "text", "text": request.prompt}],
            )
            if result.get("status") != "accepted" or result.get("disposition") not in {
                "started",
                "queued",
            }:
                raise DomainError(ErrorCode.PROTOCOL_ERROR, "Muse did not admit a new turn")
            self._normalizer.native_turn_id = result["turnId"]
            self._queued = result["disposition"] == "queued"
        except BaseException:
            await self._disconnected("Muse turn submission outcome is unknown")
            raise

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        connection = self._require(session)
        if self._normalizer.turn_id != request.turn_id or not self._normalizer.native_turn_id:
            return False
        if self._queued and not self._normalizer.turn_started:
            return False
        await connection.command(
            "turn/steer",
            sessionId=session.native_session_id,
            expectedTurnId=self._normalizer.native_turn_id,
            input=[{"type": "text", "text": request.prompt}],
        )
        return True

    async def interrupt(self, session: HarnessSession) -> None:
        connection = self._require(session)
        if self._normalizer.turn_id is not None:
            await connection.command(
                "turn/unqueue"
                if self._queued and not self._normalizer.turn_started
                else "turn/interrupt",
                sessionId=session.native_session_id,
                turnId=self._normalizer.native_turn_id,
            )

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method in {"approval/requested", "userInput/requested"}:
            await self._interaction(method, params)
        else:
            for event in self._normalizer.on_notification(method, params):
                await self._queue.put(event)

    async def _interaction(self, method: str, params: dict[str, Any]) -> None:
        turn_id = self._normalizer.turn_id
        if turn_id is None or params.get("sessionId") != self._normalizer.session_id:
            return
        if params.get("turnId") != self._normalizer.native_turn_id:
            return
        approval = method == "approval/requested"
        native_id = params["approvalId" if approval else "userInputId"]
        requirement = _dict(params.get("currentRequirementId"))
        key = (
            f"{method}:{native_id}:{requirement.get('approvalId')}:{requirement.get('sourceIndex')}"
        )
        if key in self._interaction_keys:
            return
        payload: ApprovalRequestPayload | StructuredQuestionPayload
        if approval:
            decisions = tuple(
                dict.fromkeys(
                    _DECISIONS[row["decision"]]
                    for row in map(_dict, _list(params.get("availableChoices")))
                    if row.get("decision") in _DECISIONS
                )
            )
            payload = ApprovalRequestPayload(
                tool_name=self._normalizer.redact(str(params.get("toolName") or "tool")),
                summary=self._normalizer.redact(str(params.get("rawArgs") or "")),
                available_decisions=decisions,
            )
        else:
            questions = canonical_questions(
                [
                    {
                        **row,
                        "multiSelect": _dict(row.get("selection")).get("mode") == "multiple",
                        "allowOther": True,
                    }
                    for row in map(_dict, _list(params.get("questions")))
                ]
            )
            payload = StructuredQuestionPayload(questions=questions)
        identity = uuid4()
        self._pending[identity] = (params, payload)
        self._interaction_keys.add(key)
        await self._queue.put(
            HarnessInteractionRequest(
                payload=InteractionRequestedPayload(
                    turn_id=turn_id,
                    interaction_id=identity,
                    kind=InteractionKind.APPROVAL
                    if approval
                    else InteractionKind.STRUCTURED_QUESTION,
                    request=payload,
                ),
                provider_correlation={"native_id": native_id},
            )
        )

    async def answer_interaction(self, session: HarnessSession, answer: InteractionAnswer) -> None:
        self._require(session)
        task = self._answer_tasks.get(answer.interaction_id)
        if task is None:
            # Like the SDK approval router, claim the decision before awaiting
            # IO. Concurrent delivery and replay share the original outcome,
            # including failures whose native settlement may be unknown.
            task = asyncio.create_task(self._send_answer(session, answer))
            self._answer_tasks[answer.interaction_id] = task
        await asyncio.shield(task)

    async def _send_answer(self, session: HarnessSession, answer: InteractionAnswer) -> None:
        connection = self._require(session)
        pending = self._pending.get(answer.interaction_id)
        if pending is None:
            raise DomainError(ErrorCode.INVALID_STATE, "Muse has no matching pending interaction")
        params, payload = pending
        if isinstance(payload, ApprovalRequestPayload):
            choice = next(
                (
                    row
                    for row in map(_dict, _list(params.get("availableChoices")))
                    if _DECISIONS.get(str(row.get("decision"))) == answer.decision
                ),
                None,
            )
            if choice is None or answer.decision is None:
                raise DomainError(ErrorCode.INVALID_STATE, "Muse approval decision is unavailable")
            await connection.command(
                "approval/decide",
                sessionId=session.native_session_id,
                approvalId=params["approvalId"],
                requirementId=params["currentRequirementId"],
                choiceId=choice["choiceId"],
            )
        else:
            values = canonical_answer_values(answer, payload.questions)
            answers: list[dict[str, Any]] = []
            for question in payload.questions:
                selected = values[question.id]
                native: dict[str, Any] = {"questionId": question.id}
                if any(
                    value not in {option.value for option in question.options} for value in selected
                ):
                    native["freeText"] = "\n".join(selected)
                elif question.multi_select:
                    native["selectedLabels"] = selected
                else:
                    native["selectedLabel"] = selected[0]
                answers.append(native)
            await connection.command(
                "userInput/answer",
                sessionId=session.native_session_id,
                userInputId=params["userInputId"],
                answers=answers,
            )
        del self._pending[answer.interaction_id]

    async def _disconnected(self, message: str) -> None:
        if not self._closed:
            for event in self._normalizer.disconnected(message):
                await self._queue.put(event)

    def events(
        self, session: HarnessSession
    ) -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
        self._require(session)

        async def stream() -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
            while (item := await self._queue.get()) is not None:
                yield item

        return stream()

    async def close(self, session: HarnessSession) -> None:
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            await self._connection.close()
        await asyncio.gather(*self._answer_tasks.values(), return_exceptions=True)
        await self._queue.put(None)
        if self._config_dir is not None:
            remove_config_dir(self._config_dir)
            self._config_dir = None
