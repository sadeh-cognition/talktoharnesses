"""Provider-neutral interaction request/resolution broker.

Adapters emit requests; the broker owns durable policy, first-write-wins
resolution, and publication-gated answer_interaction command release.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from talktoharnesses.application.persistence import Persistence
from talktoharnesses.application.publisher import CommittedEventPublisher
from talktoharnesses.domain.approval_matching import (
    InteractionMatchContext,
    normalize_approval_action,
    normalize_approval_rule,
    rule_matches_request,
)
from talktoharnesses.domain.enums import (
    ApprovalDecision,
    ApprovalRuleDecision,
    CommandKind,
    CommandStatus,
    ErrorCode,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    AnswerInteractionPayload,
    ApprovalRequestPayload,
    ApprovalRule,
    Command,
    CommandProjection,
    InteractionAnswer,
    InteractionProjection,
    PendingInteraction,
)
from talktoharnesses.domain.transitions import (
    cancel_interaction,
    request_interaction,
    submit_interaction_answer,
    update_interaction_draft,
)

logger = logging.getLogger(__name__)


def _command_projection(command: Command) -> CommandProjection:
    return CommandProjection(
        id=command.id,
        kind=command.kind,
        status=command.status,
        target_turn_id=command.target_turn_id,
        idempotency_key=command.idempotency_key,
        created_at=command.created_at,
    )


class InteractionBroker:
    """Single owner of interaction commit, rule evaluation, and answer release."""

    def __init__(
        self,
        persistence: Persistence,
        publisher: CommittedEventPublisher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._persistence = persistence
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    async def accept_request(
        self,
        conversation_id: UUID,
        interaction: PendingInteraction,
        *,
        provider_correlation: Mapping[str, str] | None = None,
    ) -> None:
        """Force-commit a request, publish, then evaluate automatic policy."""
        state = await self._persistence.get_worker_snapshot(conversation_id)
        if isinstance(interaction.request, ApprovalRequestPayload):
            try:
                action = normalize_approval_action(
                    interaction.request.action,
                    working_directory=self._working_directory(state),
                )
            except ValueError:
                action = None
            interaction = interaction.model_copy(
                update={"request": interaction.request.model_copy(update={"action": action})}
            )
        result = request_interaction(state, interaction, now=self._clock())
        if not result.events:
            unevaluated = await self._persistence.list_unevaluated_open_interactions()
            if (conversation_id, interaction.id) not in unevaluated:
                return
            await self._publish_request(conversation_id, interaction.id)
            await self._evaluate_policy(conversation_id, interaction.id)
            return

        request_seq = next(
            (e.sequence for e in result.events if e.type == "interaction_requested"),
            result.events[0].sequence,
        )
        committed = await self._persistence.commit_interaction_request(
            conversation_id,
            state.conversation.version,
            result.state,
            result.events,
            interaction_id=interaction.id,
            provider_correlation=dict(provider_correlation or {}),
            request_event_sequence=request_seq,
        )
        try:
            await self._publisher.publish(committed)
        except Exception:
            logger.exception("failed to publish interaction request; leave unevaluated")
            return
        await self._evaluate_policy(conversation_id, interaction.id)

    async def update_draft(
        self,
        owner_id: str,
        conversation_id: UUID,
        interaction_id: UUID,
        *,
        draft: dict[str, Any],
    ) -> InteractionProjection:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        if interaction_id not in state.interactions:
            raise DomainError(ErrorCode.NOT_FOUND, "interaction not found")
        result = update_interaction_draft(
            state,
            interaction_id=interaction_id,
            draft=draft,
            now=self._clock(),
        )
        events = await self._persistence.commit_facade_mutation(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
        )
        await self._publisher.publish(events)
        interaction = result.state.interactions[interaction_id]
        return InteractionProjection(
            id=interaction.id,
            kind=interaction.kind,
            status=interaction.status,
            turn_id=interaction.turn_id,
            request=interaction.request,
            draft=interaction.draft,
            created_at=interaction.created_at,
        )

    async def resolve_manual(
        self,
        owner_id: str,
        conversation_id: UUID,
        interaction_id: UUID,
        *,
        decision: ApprovalDecision | None = None,
        answers: dict[str, Any] | None = None,
        create_rule: ApprovalRule | None = None,
        idempotency_key: str | None = None,
    ) -> CommandProjection:
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        if interaction_id not in state.interactions:
            raise DomainError(ErrorCode.NOT_FOUND, "interaction not found")

        existing = self._find_answer_command(state, interaction_id)
        if interaction_id in state.answers and not state.answers[interaction_id].is_draft:
            if existing is not None:
                return _command_projection(existing)
            await self._publish_resolution(conversation_id, interaction_id)
            state = await self._persistence.get_snapshot(conversation_id, owner_id)
            return await self._release_for(conversation_id, owner_id, interaction_id, state)

        if create_rule is not None:
            if create_rule.principal_id != owner_id:
                raise DomainError(
                    ErrorCode.INVALID_STATE,
                    "rule principal must match caller",
                )
            try:
                create_rule = normalize_approval_rule(
                    create_rule,
                    working_directory=self._working_directory(state),
                )
            except ValueError as exc:
                raise DomainError(ErrorCode.INVALID_STATE, str(exc)) from exc
            self._validate_create_and_allow(state, interaction_id, create_rule, decision)

        answer = InteractionAnswer(
            interaction_id=interaction_id,
            decision=decision,
            answers=answers,
        )
        result = submit_interaction_answer(
            state,
            answer,
            now=self._clock(),
            automatic=False,
        )
        if not result.events:
            state = await self._persistence.get_snapshot(conversation_id, owner_id)
            existing = self._find_answer_command(state, interaction_id)
            if existing is not None:
                return _command_projection(existing)
            raise DomainError(
                ErrorCode.INTERACTION_ALREADY_RESOLVED,
                "interaction already resolved",
            )

        submitted = result.state.answers[interaction_id]
        resolution_seq = result.events[-1].sequence
        await self._persistence.commit_interaction_resolution(
            conversation_id,
            owner_id,
            state.conversation.version,
            result.state,
            result.events,
            submitted,
            automatic=False,
            create_rule=create_rule,
            deciding_rule=create_rule,
            resolution_event_sequence=resolution_seq,
        )
        await self._publish_resolution(conversation_id, interaction_id)
        state = await self._persistence.get_snapshot(conversation_id, owner_id)
        existing = self._find_answer_command(state, interaction_id)
        if existing is not None:
            return _command_projection(existing)
        return await self._release_for(
            conversation_id,
            owner_id,
            interaction_id,
            state,
            idempotency_key=idempotency_key,
        )

    async def cancel_open_for_interrupt(self, conversation_id: UUID) -> None:
        """Durably cancel open interactions and publish before adapter interrupt."""
        state = await self._persistence.get_worker_snapshot(conversation_id)
        owner_id = state.conversation.owner_id
        open_ids = [interaction.id for interaction in state.interactions.values()]
        for interaction_id in open_ids:
            state = await self._persistence.get_worker_snapshot(conversation_id)
            if interaction_id in state.answers:
                continue
            result = cancel_interaction(
                state,
                interaction_id=interaction_id,
                now=self._clock(),
            )
            if not result.events:
                continue
            submitted = result.state.answers[interaction_id]
            resolution = await self._persistence.commit_interaction_resolution(
                conversation_id,
                owner_id,
                state.conversation.version,
                result.state,
                result.events,
                submitted,
                automatic=False,
                resolution_event_sequence=result.events[-1].sequence,
                suppress_answer_command=True,
            )
            if resolution.was_first_write:
                await self._publish_resolution(conversation_id, interaction_id)
                await self._persistence.complete_suppressed_interaction_resolution(
                    interaction_id,
                    self._clock(),
                )

    async def reconcile_on_startup(self) -> None:
        """Republish/evaluate unevaluated requests and release unreleased answers."""
        unevaluated = await self._persistence.list_unevaluated_open_interactions()
        for conversation_id, interaction_id in unevaluated:
            try:
                state = await self._persistence.get_worker_snapshot(conversation_id)
                interaction = state.interactions.get(interaction_id)
                if interaction is None:
                    continue
                await self._publish_request(conversation_id, interaction_id)
                await self._evaluate_policy(conversation_id, interaction_id)
            except Exception:
                logger.exception(
                    "reconcile failed for interaction %s in %s",
                    interaction_id,
                    conversation_id,
                )

        unreleased = await self._persistence.list_unreleased_resolutions()
        for conversation_id, interaction_id in unreleased:
            try:
                state = await self._persistence.get_worker_snapshot(conversation_id)
                owner_id = state.conversation.owner_id
                await self._publish_resolution(conversation_id, interaction_id)
                if await self._persistence.complete_suppressed_interaction_resolution(
                    interaction_id,
                    self._clock(),
                ):
                    continue
                state = await self._persistence.get_worker_snapshot(conversation_id)
                await self._release_for(conversation_id, owner_id, interaction_id, state)
            except Exception:
                logger.exception(
                    "release reconcile failed for interaction %s in %s",
                    interaction_id,
                    conversation_id,
                )

    async def _evaluate_policy(self, conversation_id: UUID, interaction_id: UUID) -> None:
        state = await self._persistence.get_worker_snapshot(conversation_id)
        interaction = state.interactions.get(interaction_id)
        if interaction is None:
            return
        if interaction_id in state.answers and not state.answers[interaction_id].is_draft:
            return

        owner_id = state.conversation.owner_id
        resolution = await self._persistence.commit_interaction_resolution(
            conversation_id,
            owner_id,
            state.conversation.version,
            state,
            (),
            InteractionAnswer(interaction_id=interaction_id, submitted_at=self._clock()),
            automatic=True,
            resolution_event_sequence=0,
            mark_policy_evaluated=True,
        )
        if not resolution.was_first_write:
            return
        await self._publish_resolution(conversation_id, interaction_id)
        state_after = await self._persistence.get_worker_snapshot(conversation_id)
        await self._release_for(conversation_id, owner_id, interaction_id, state_after)

    async def _publish_resolution(self, conversation_id: UUID, interaction_id: UUID) -> None:
        event = await self._persistence.get_interaction_resolution_event(
            conversation_id,
            interaction_id,
        )
        await self._publisher.publish((event,))

    async def _publish_request(self, conversation_id: UUID, interaction_id: UUID) -> None:
        event = await self._persistence.get_interaction_request_event(
            conversation_id,
            interaction_id,
        )
        await self._publisher.publish((event,))

    @staticmethod
    def _working_directory(state: Any) -> str | None:
        if state.binding is None or state.binding.launch_snapshot is None:
            return None
        return state.binding.launch_snapshot.working_directory

    async def _release_for(
        self,
        conversation_id: UUID,
        owner_id: str,
        interaction_id: UUID,
        state: Any,
        *,
        idempotency_key: str | None = None,
    ) -> CommandProjection:
        existing = self._find_answer_command(state, interaction_id)
        if existing is not None:
            return _command_projection(existing)
        interaction = state.interactions.get(interaction_id)
        turn_id = interaction.turn_id if interaction is not None else None
        now = self._clock()
        command = Command(
            id=uuid4(),
            conversation_id=conversation_id,
            kind=CommandKind.ANSWER_INTERACTION,
            status=CommandStatus.ACCEPTED,
            idempotency_key=idempotency_key or f"answer-interaction:{interaction_id}",
            target_turn_id=turn_id,
            payload=AnswerInteractionPayload(interaction_id=interaction_id),
            created_at=now,
        )
        commands = dict(state.commands)
        commands[command.id] = command
        released_state = state.model_copy(update={"commands": commands})
        released = await self._persistence.release_interaction_answer(
            conversation_id,
            owner_id,
            interaction_id,
            command,
            expected_version=state.conversation.version,
            state=released_state,
        )
        return _command_projection(released)

    @staticmethod
    def _find_answer_command(state: Any, interaction_id: UUID) -> Command | None:
        for cmd in state.commands.values():
            if (
                cmd.kind is CommandKind.ANSWER_INTERACTION
                and isinstance(cmd.payload, AnswerInteractionPayload)
                and cmd.payload.interaction_id == interaction_id
            ):
                return cmd
        return None

    @staticmethod
    def _validate_create_and_allow(
        state: Any,
        interaction_id: UUID,
        rule: ApprovalRule,
        decision: ApprovalDecision | None,
    ) -> None:
        if decision is not ApprovalDecision.ALLOW_ONCE:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "create-and-allow requires decision allow_once",
            )
        interaction = state.interactions[interaction_id]
        action = None
        if isinstance(interaction.request, ApprovalRequestPayload):
            action = interaction.request.action
        ctx = InteractionMatchContext(
            principal_id=rule.principal_id,
            conversation_id=state.conversation.id,
            owner_id=state.conversation.owner_id,
            binding=state.binding,
            working_directory=InteractionBroker._working_directory(state),
        )
        if not rule_matches_request(rule, action=action, ctx=ctx):
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "create-and-allow rule does not match the current request",
            )
        if rule.decision is not ApprovalRuleDecision.ALLOW:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "create-and-allow requires an allow rule",
            )
