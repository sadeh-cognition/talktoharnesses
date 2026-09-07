"""Muse Code HarnessAdapter using the official MSP command and event planes."""

from __future__ import annotations

import asyncio
import logging
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
from tth_muse.harness.wire_log import WireLog
from tth_muse.runtime.handle import ProcessHandle
from tth_muse.shared.questions import canonical_answer_values, canonical_questions

logger = logging.getLogger(__name__)

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


def _interaction_key(method: str, params: dict[str, Any]) -> str:
    native_id = params.get("approvalId" if method == "approval/requested" else "userInputId")
    requirement = _dict(params.get("currentRequirementId"))
    return f"{method}:{native_id}:{requirement.get('approvalId')}:{requirement.get('sourceIndex')}"


class MuseAdapter:
    kind = HarnessKind.MUSE

    def __init__(
        self,
        *,
        push_stall_probe: float = 20.0,
        push_poll_interval: float = 2.0,
        push_recovery_page_limit: int = 100,
        push_recovery_max_pages: int = 20,
    ) -> None:
        self._push_stall_probe = push_stall_probe
        self._push_poll_interval = push_poll_interval
        self._push_recovery_page_limit = push_recovery_page_limit
        self._push_recovery_max_pages = push_recovery_max_pages
        self._watchdog: asyncio.Task[None] | None = None
        self._notify_lock = asyncio.Lock()
        self.push_recoveries = 0
        # Once the host's push delivery has died for a turn it has not come
        # back (session/resume answers with projectionUnavailable), so after
        # the first confirmed stall the watchdog polls the view at the short
        # interval instead of waiting out the probe window each time.
        self.polling = False
        self._resubscribe_failed = False
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
        self._wire: WireLog | None = None

    def bind_process(self, process: ProcessHandle) -> None:
        self._process = process

    def attach_wire_log(self, wire: WireLog | None) -> None:
        """Capture raw MSP frames for this adapter's connection (see wire_log)."""
        self._wire = wire

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
                self._process,
                self._notification,
                self._disconnected,
                self._normalizer.redact,
                wire=self._wire,
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
        logger.info(
            "muse session bound native_session_id=%s conversation=%s binding=%s model=%s",
            native["sessionId"],
            request.conversation_id,
            request.binding_id,
            self._model,
        )
        if self._wire is not None:
            self._wire.note(
                "session bound",
                native_session_id=native["sessionId"],
                conversation_id=str(request.conversation_id),
                binding_id=str(request.binding_id),
            )
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
        self.polling = False
        self._resubscribe_failed = False
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
        self._start_watchdog(session, connection)

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
        logger.info(
            "muse interrupt requested turn_id=%s native_turn_id=%s queued=%s started=%s",
            self._normalizer.turn_id,
            self._normalizer.native_turn_id,
            self._queued,
            self._normalizer.turn_started,
        )
        if self._normalizer.turn_id is not None:
            await connection.command(
                "turn/unqueue"
                if self._queued and not self._normalizer.turn_started
                else "turn/interrupt",
                sessionId=session.native_session_id,
                turnId=self._normalizer.native_turn_id,
            )

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        # The reader loop and the push watchdog's replay both feed the
        # normalizer; serialize them so an item's state transitions in order.
        async with self._notify_lock:
            await self._apply_notification(method, params)

    async def _apply_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in {"approval/requested", "userInput/requested"}:
            await self._interaction(method, params)
        else:
            events = self._normalizer.on_notification(method, params)
            if not events:
                # Not an error: the normalizer ignores frames for other
                # sessions/turns and unfamiliar methods. Logged because a
                # frame the host sent and nobody forwarded is exactly what a
                # stalled turn looks like from the proxy.
                self._log_drop("notification", method, params)
            for event in events:
                await self._queue.put(event)

    def _log_drop(self, what: str, method: str, params: dict[str, Any]) -> None:
        reason = self._normalizer.drop_reason(params)
        # A frame filtered out because of *which* session/turn it belongs to
        # is the interesting case. Methods with no TTH mapping and host
        # notifications between turns are routine and stay at DEBUG.
        level = logging.INFO if reason in {"other_session", "other_turn"} else logging.DEBUG
        logger.log(
            level,
            "muse %s not forwarded method=%s reason=%s turnId=%s sessionId=%s",
            what,
            method,
            reason,
            params.get("turnId"),
            params.get("sessionId"),
        )
        if self._wire is not None:
            self._wire.note(
                f"{what} not forwarded",
                method=method,
                reason=reason,
                turn_id=params.get("turnId"),
                session_id=params.get("sessionId"),
            )

    async def _interaction(self, method: str, params: dict[str, Any]) -> None:
        turn_id = self._normalizer.turn_id
        if turn_id is None or params.get("sessionId") != self._normalizer.session_id:
            self._log_drop("interaction", method, params)
            return
        if params.get("turnId") != self._normalizer.native_turn_id:
            self._log_drop("interaction", method, params)
            return
        approval = method == "approval/requested"
        native_id = params["approvalId" if approval else "userInputId"]
        key = _interaction_key(method, params)
        if key in self._interaction_keys:
            logger.info("muse interaction already forwarded key=%s", key)
            return
        logger.info(
            "muse interaction forwarded method=%s native_id=%s turnId=%s",
            method,
            native_id,
            params.get("turnId"),
        )
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

    # -- push-subscription watchdog -------------------------------------------
    #
    # Muse's push delivery to a subscribed connection dies silently after a
    # few hundred view events in a busy turn (observed twice on 2026-09-06:
    # no view/gap, no error, the host keeps working and journals everything,
    # the connection stays usable for requests, but nothing is pushed again).
    # Paged reads of the same view keep working, so a stalled turn is checked
    # against them and, when the subscription is dead, re-established with
    # session/resume while the missed durable events are replayed.

    def _start_watchdog(self, session: HarnessSession, connection: MuseConnection) -> None:
        self._stop_watchdog()
        self._watchdog = asyncio.create_task(
            self._push_watchdog(session, connection), name="muse-push-watchdog"
        )

    def _stop_watchdog(self) -> None:
        task, self._watchdog = self._watchdog, None
        if task is not None and not task.done():
            task.cancel()

    def _turn_active(self) -> bool:
        # A method call rather than an inline check: the turn ends inside
        # awaits the type checker cannot see through.
        return not self._closed and self._normalizer.turn_id is not None

    async def _push_watchdog(self, session: HarnessSession, connection: MuseConnection) -> None:
        seen_frames = connection.frames_in
        self.polling = False
        try:
            while self._turn_active():
                await asyncio.sleep(
                    self._push_poll_interval if self.polling else self._push_stall_probe
                )
                if not self._turn_active():
                    return
                if connection.frames_in != seen_frames:
                    seen_frames = connection.frames_in
                    if self.polling:
                        # A frame arrived on its own: push delivery is back.
                        logger.info(
                            "muse push delivery resumed on turn %s; leaving polling mode",
                            self._normalizer.native_turn_id,
                        )
                        self.polling = False
                    continue
                try:
                    recovered = await self._recover_push(session, connection)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a failed probe must not end the turn
                    logger.exception("muse push watchdog probe failed")
                    continue
                seen_frames = connection.frames_in
                if recovered:
                    self.push_recoveries += 1
        except asyncio.CancelledError:
            return

    async def _page_backward(
        self, connection: MuseConnection, native_session_id: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """Newest-first pages of the view until an already-forwarded event.

        Returns the unseen events in delivery order; empty when the view holds
        nothing beyond what push delivery already brought.
        """
        unseen: list[tuple[str, dict[str, Any]]] = []
        cursor: str | None = None
        for _ in range(self._push_recovery_max_pages):
            params: dict[str, Any] = {
                "sessionId": native_session_id,
                "direction": "backward",
                "limit": self._push_recovery_page_limit,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await connection.request("view/page", **params)
            events = [
                (str(row["method"]), _dict(row.get("params")))
                for row in map(_dict, _list(result.get("events")))
                if isinstance(row.get("method"), str)
            ]
            if not events:
                break
            page_unseen: list[tuple[str, dict[str, Any]]] = []
            hit_known = False
            for method, event_params in reversed(events):
                verdict = self._classify_event(method, event_params)
                if verdict == "new":
                    page_unseen.append((method, event_params))
                elif verdict == "known":
                    hit_known = True
                    break
                # "irrelevant" (other turns, unmapped item kinds, the fold's
                # provisional rendering of the open turn) neither proves nor
                # disproves delivery of what lies beneath it.
            unseen = list(reversed(page_unseen)) + unseen
            next_cursor = result.get("nextCursor")
            if hit_known or not isinstance(next_cursor, str) or next_cursor == cursor:
                break
            cursor = next_cursor
        return unseen

    def _classify_event(self, method: str, params: dict[str, Any]) -> str:
        if method in {"approval/requested", "userInput/requested"}:
            if self._normalizer.turn_id is None:
                return "irrelevant"
            if params.get("sessionId") != self._normalizer.session_id:
                return "irrelevant"
            if params.get("turnId") != self._normalizer.native_turn_id:
                return "irrelevant"
            if _interaction_key(method, params) in self._interaction_keys:
                return "known"
            return "new"
        return self._normalizer.classify(method, params)

    async def _recover_push(self, session: HarnessSession, connection: MuseConnection) -> bool:
        native_session_id = session.native_session_id
        if not native_session_id:
            return False
        unseen = await self._page_backward(connection, native_session_id)
        if not unseen:
            if not self.polling:
                logger.info(
                    "muse turn %s quiet for %.0fs; view has nothing new (push subscription alive)",
                    self._normalizer.native_turn_id,
                    self._push_stall_probe,
                )
            return False
        methods = [method for method, _ in unseen]
        if self.polling:
            logger.info(
                "muse turn %s: polled %d view events (%s)",
                self._normalizer.native_turn_id,
                len(unseen),
                ", ".join(sorted(set(methods))),
            )
        else:
            logger.warning(
                "muse push subscription stalled on turn %s: view holds %d undelivered events "
                "(%s); replaying and polling every %.0fs",
                self._normalizer.native_turn_id,
                len(unseen),
                ", ".join(sorted(set(methods))),
                self._push_poll_interval,
            )
            if self._wire is not None:
                self._wire.note(
                    "push subscription stalled",
                    native_turn_id=self._normalizer.native_turn_id,
                    undelivered=len(unseen),
                    methods=sorted(set(methods)),
                    last_view_cursor=self._normalizer.last_view_cursor,
                )
            self.polling = True
            if not self._resubscribe_failed:
                # Worth one try per turn: cheap, and it does restore push on a
                # healthy host. It has not been seen to revive a dead one.
                self._resubscribe_failed = not await self._resubscribe(
                    connection, native_session_id
                )
        for method, params in unseen:
            replay = {key: value for key, value in params.items() if key != "viewCursor"}
            await self._notification(method, replay)
        return True

    async def _resubscribe(self, connection: MuseConnection, native_session_id: str) -> bool:
        """Re-attach this connection to the session's view subscription.

        ``session/resume`` on an already-loaded session is accepted by the host
        and subscribes the connection after the given cursor; it serves no
        history for a cursor resume, which is why the gap is replayed from
        paged reads instead. Best effort: a refused resume leaves the turn on
        paged polling, which the watchdog keeps doing every probe interval.
        """
        params: dict[str, Any] = {"sessionId": native_session_id, "excludeItems": True}
        cursor = self._normalizer.last_view_cursor
        if cursor:
            params["cursor"] = cursor
        try:
            result = await connection.command("session/resume", **params)
        except DomainError as exc:
            # A cursor last observed on an ephemeral push (item/started,
            # deltas) is not always an anchor the host accepts; the
            # subscription matters more than the anchor, so retry from the
            # head. The gap is replayed from paged reads regardless.
            if cursor and exc.details.get("native_error_reason") == "missingAnchor":
                logger.info("muse session/resume rejected cursor %s; resuming from head", cursor)
                del params["cursor"]
                try:
                    result = await connection.command("session/resume", **params)
                except DomainError as retry_exc:
                    logger.warning("muse session/resume for re-subscription refused: %s", retry_exc)
                    return False
            else:
                logger.warning("muse session/resume for re-subscription refused: %s", exc)
                return False
        native = _dict(result.get("session"))
        if native.get("sessionId") != native_session_id:
            logger.warning("muse session/resume re-subscribed a different session; ignoring")
            return False
        history = _dict(result.get("history"))
        logger.info(
            "muse re-subscribed session %s after cursor %s (active turn %s, history %s)",
            native_session_id,
            self._normalizer.last_view_cursor,
            native.get("activeTurnId"),
            history.get("noneReason") or history.get("mode"),
        )
        # ``projectionUnavailable`` is what a host whose view projection has
        # died answers; the subscription it grants never delivers.
        return history.get("noneReason") != "projectionUnavailable"

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
        self._stop_watchdog()
        if self._connection is not None:
            await self._connection.close()
        await asyncio.gather(*self._answer_tasks.values(), return_exceptions=True)
        await self._queue.put(None)
        if self._config_dir is not None:
            remove_config_dir(self._config_dir)
            self._config_dir = None
        if self._wire is not None:
            self._wire.close("adapter closed")
            self._wire = None
