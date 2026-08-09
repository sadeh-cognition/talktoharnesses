"""SQLite contract checks for the production persistence implementation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import (
    ApprovalDecision,
    ApprovalRule,
    ApprovalRuleDecision,
    CommandApprovalAction,
    CommandKind,
    CommandStatus,
    DomainError,
    ErrorCode,
    ExactArgvMatcher,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    LaunchSnapshot,
    PrincipalGlobalRuleScope,
    ProcessRecord,
    ProcessStatus,
    append_events,
    new_conversation_state,
    pin_conversation,
    request_interaction,
    start_turn,
    submit_interaction_answer,
    submit_turn,
)
from talktoharnesses.domain.enums import InteractionKind
from talktoharnesses.domain.events import ProcessExitedPayload
from talktoharnesses.domain.models import (
    AnswerInteractionPayload,
    ApprovalRequestPayload,
    Command,
    ConversationHarnessBinding,
    InteractionAnswer,
    PendingInteraction,
    SubmitTurnPayload,
)
from talktoharnesses.domain.transitions import ConversationState


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_runtime_lifecycle_round_trip_and_conflict() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    conversation_id = uuid4()
    configuration = HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        working_directory="/tmp",
    )
    binding = ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.OPENCODE,
        configuration=configuration,
        created_at=now,
    )
    state = new_conversation_state(
        owner_id="owner-1",
        now=now,
        binding=binding,
        conversation_id=conversation_id,
    )
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)

    capabilities = HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1")
    launch = LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1",
        working_directory="/tmp",
        adapter_version="1",
        capabilities=capabilities,
    )
    process = ProcessRecord(
        conversation_id=conversation_id,
        binding_id=binding.id,
        status=ProcessStatus.RUNNING,
        pid=123,
        started_at=now,
    )
    state = state.model_copy(
        update={"binding": binding.model_copy(update={"launch_snapshot": launch})}
    )
    await persistence.commit_runtime_lifecycle(
        conversation_id,
        0,
        state,
        process,
        launch,
        (),
    )

    final_state, events = append_events(
        state,
        now,
        [ProcessExitedPayload(process_id=process.id, exit_code=0)],
    )
    exited = process.model_copy(
        update={"status": ProcessStatus.EXITED, "exit_code": 0, "exited_at": now}
    )
    await persistence.commit_runtime_lifecycle(
        conversation_id,
        0,
        final_state,
        exited,
        None,
        events,
    )

    loaded = await persistence.get_snapshot(conversation_id, "owner-1")
    assert loaded == final_state
    assert tuple(await persistence.replay(conversation_id, 0, 10, 100_000)) == events
    with pytest.raises(DomainError) as exc_info:
        await persistence.commit_runtime_lifecycle(
            conversation_id,
            0,
            final_state,
            exited,
            None,
            (),
        )
    assert exc_info.value.code is ErrorCode.OPTIMISTIC_CONFLICT


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_claim_reclaims_only_expired_pre_delivery_command() -> None:
    now = datetime.now(UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)
    command = Command(
        conversation_id=state.conversation.id,
        kind=CommandKind.SUBMIT_TURN,
        status=CommandStatus.CLAIMED,
        idempotency_key="expired",
        worker_id="dead-worker",
        lease_expires_at=now - timedelta(seconds=1),
        attempts=1,
        payload=SubmitTurnPayload(prompt="retry"),
        created_at=now,
    )
    await persistence.accept_command(command)

    claimed = await persistence.claim_commands("live-worker", 1, lease_duration=30.0)

    assert len(claimed) == 1
    assert claimed[0].command.worker_id == "live-worker"
    assert claimed[0].command.attempts == 2
    assert claimed[0].fence >= 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_answer_release_merges_into_locked_newer_aggregate() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)

    queued = submit_turn(state, prompt="x", idempotency_key="turn", now=now)
    running = start_turn(queued.state, now=now)
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.CANCEL)
        ),
        created_at=now,
    )
    requested = request_interaction(running.state, interaction, now=now)
    request_events = (*queued.events, *running.events, *requested.events)
    await persistence.commit_interaction_request(
        state.conversation.id,
        state.conversation.version,
        requested.state,
        request_events,
        interaction_id=interaction.id,
        request_event_sequence=requested.events[-1].sequence,
    )

    resolved = submit_interaction_answer(
        requested.state,
        InteractionAnswer(
            interaction_id=interaction.id,
            decision=ApprovalDecision.ALLOW_ONCE,
        ),
        now=now,
    )
    answer = resolved.state.answers[interaction.id]
    await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        resolved.state,
        resolved.events,
        answer,
        resolution_event_sequence=resolved.events[-1].sequence,
    )

    newer = pin_conversation(resolved.state, now=now)
    await persistence.commit_event_batch(
        state.conversation.id,
        resolved.state.conversation.version,
        newer.state,
        newer.events,
    )
    command = Command(
        conversation_id=state.conversation.id,
        kind=CommandKind.ANSWER_INTERACTION,
        status=CommandStatus.ACCEPTED,
        idempotency_key=f"answer-interaction:{interaction.id}",
        target_turn_id=interaction.turn_id,
        payload=AnswerInteractionPayload(interaction_id=interaction.id),
        created_at=now,
    )
    stale_commands = dict(resolved.state.commands)
    stale_commands[command.id] = command
    await persistence.release_interaction_answer(
        state.conversation.id,
        "owner",
        interaction.id,
        command,
        expected_version=resolved.state.conversation.version,
        state=resolved.state.model_copy(update={"commands": stale_commands}),
    )

    loaded = await persistence.get_snapshot(state.conversation.id, "owner")
    assert loaded.conversation.version == newer.state.conversation.version
    assert loaded.conversation.pinned_at == newer.state.conversation.pinned_at
    assert loaded.commands[command.id] == command


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_automatic_resolution_selects_live_rule_in_transaction() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)
    rule = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "arg")),
        created_at=now,
        updated_at=now,
    )
    await persistence.create_approval_rule(rule)
    queued = submit_turn(state, prompt="x", idempotency_key="turn", now=now)
    running = start_turn(queued.state, now=now)
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            action=CommandApprovalAction(argv=("tool", "arg")),
            available_decisions=(
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            ),
        ),
        created_at=now,
    )
    requested = request_interaction(running.state, interaction, now=now)
    request_events = (*queued.events, *running.events, *requested.events)
    await persistence.commit_interaction_request(
        state.conversation.id,
        state.conversation.version,
        requested.state,
        request_events,
        interaction_id=interaction.id,
        provider_correlation={"json_rpc_request_id": "p1"},
        request_event_sequence=requested.events[-1].sequence,
    )

    result = await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        requested.state,
        (),
        InteractionAnswer(interaction_id=interaction.id, submitted_at=now),
        automatic=True,
        resolution_event_sequence=0,
        mark_policy_evaluated=True,
    )

    assert result.was_first_write is True
    assert result.answer.decision is ApprovalDecision.ALLOW_ONCE
    assert result.audit is not None
    assert result.audit.deciding_rule_id == rule.id
    assert result.audit.provider_request_ids == {"json_rpc_request_id": "p1"}
    event = await persistence.get_interaction_resolution_event(
        state.conversation.id, interaction.id
    )
    assert event.type == "interaction_resolved"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_first_write_wins_resolution_race() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)
    queued = submit_turn(state, prompt="x", idempotency_key="turn", now=now)
    running = start_turn(queued.state, now=now)
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY)
        ),
        created_at=now,
    )
    requested = request_interaction(running.state, interaction, now=now)
    await persistence.commit_interaction_request(
        state.conversation.id,
        state.conversation.version,
        requested.state,
        (*queued.events, *running.events, *requested.events),
        interaction_id=interaction.id,
        request_event_sequence=requested.events[-1].sequence,
    )
    allow = submit_interaction_answer(
        requested.state,
        InteractionAnswer(interaction_id=interaction.id, decision=ApprovalDecision.ALLOW_ONCE),
        now=now,
    )
    deny = submit_interaction_answer(
        requested.state,
        InteractionAnswer(interaction_id=interaction.id, decision=ApprovalDecision.DENY),
        now=now,
    )
    first = await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        allow.state,
        allow.events,
        allow.state.answers[interaction.id],
        resolution_event_sequence=allow.events[-1].sequence,
    )
    second = await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        deny.state,
        deny.events,
        deny.state.answers[interaction.id],
        resolution_event_sequence=deny.events[-1].sequence if deny.events else 0,
    )
    assert first.was_first_write is True
    assert second.was_first_write is False
    assert second.answer.decision is ApprovalDecision.ALLOW_ONCE


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_audit_survives_rule_delete_and_conversation_soft_delete() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)
    rule = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool",)),
        created_at=now,
        updated_at=now,
    )
    await persistence.create_approval_rule(rule)
    queued = submit_turn(state, prompt="x", idempotency_key="turn", now=now)
    running = start_turn(queued.state, now=now)
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            action=CommandApprovalAction(argv=("tool",)),
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
        ),
        created_at=now,
    )
    requested = request_interaction(running.state, interaction, now=now)
    await persistence.commit_interaction_request(
        state.conversation.id,
        state.conversation.version,
        requested.state,
        (*queued.events, *running.events, *requested.events),
        interaction_id=interaction.id,
        request_event_sequence=requested.events[-1].sequence,
    )
    result = await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        requested.state,
        (),
        InteractionAnswer(interaction_id=interaction.id, submitted_at=now),
        automatic=True,
        resolution_event_sequence=0,
        mark_policy_evaluated=True,
    )
    assert result.audit is not None
    audit_id = result.audit.id

    await persistence.delete_approval_rule(rule.id, "owner")
    with pytest.raises(DomainError) as missing_rule:
        await persistence.get_approval_rule(rule.id, "owner")
    assert missing_rule.value.code is ErrorCode.NOT_FOUND

    audit = await persistence.get_interaction_audit(audit_id, "owner")
    assert audit.deciding_rule_id == rule.id
    assert audit.rule_decision is ApprovalRuleDecision.ALLOW
    assert audit.rule_matcher is not None

    # Soft-delete conversation after turn completes; audit remains owner-queryable.
    from talktoharnesses.domain import complete_turn, soft_delete_conversation

    loaded = await persistence.get_snapshot(state.conversation.id, "owner")
    if loaded.active_turn is not None:
        finished = complete_turn(loaded, now=now)
        await persistence.commit_facade_mutation(
            state.conversation.id,
            "owner",
            loaded.conversation.version,
            finished.state,
            finished.events,
        )
        loaded = finished.state
    deleted = soft_delete_conversation(loaded, now=now)
    await persistence.commit_facade_mutation(
        state.conversation.id,
        "owner",
        loaded.conversation.version,
        deleted.state,
        deleted.events,
    )
    still = await persistence.get_interaction_audit(audit_id, "owner")
    assert still.id == audit_id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rule_replace_during_evaluation_yields_coherent_audit() -> None:
    """Evaluation locks rules; audit snapshot is entirely before or after replace."""
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)
    rule = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "coherent")),
        created_at=now,
        updated_at=now,
    )
    await persistence.create_approval_rule(rule)
    queued = submit_turn(state, prompt="x", idempotency_key="turn", now=now)
    running = start_turn(queued.state, now=now)
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            action=CommandApprovalAction(argv=("tool", "coherent")),
            available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
        ),
        created_at=now,
    )
    requested = request_interaction(running.state, interaction, now=now)
    await persistence.commit_interaction_request(
        state.conversation.id,
        state.conversation.version,
        requested.state,
        (*queued.events, *running.events, *requested.events),
        interaction_id=interaction.id,
        request_event_sequence=requested.events[-1].sequence,
    )
    # Replace before evaluation: deny.
    replaced = rule.model_copy(update={"decision": ApprovalRuleDecision.DENY, "updated_at": now})
    await persistence.replace_approval_rule(replaced)
    result = await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        requested.state,
        (),
        InteractionAnswer(interaction_id=interaction.id, submitted_at=now),
        automatic=True,
        resolution_event_sequence=0,
        mark_policy_evaluated=True,
    )
    assert result.was_first_write is True
    assert result.answer.decision is ApprovalDecision.DENY
    assert result.audit is not None
    assert result.audit.rule_decision is ApprovalRuleDecision.DENY
    assert result.audit.deciding_rule_id == rule.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_and_allow_loser_does_not_leave_orphan_rule() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)
    queued = submit_turn(state, prompt="x", idempotency_key="turn", now=now)
    running = start_turn(queued.state, now=now)
    interaction = PendingInteraction(
        conversation_id=state.conversation.id,
        turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
        kind=InteractionKind.APPROVAL,
        request=ApprovalRequestPayload(
            action=CommandApprovalAction(argv=("tool", "x")),
            available_decisions=(ApprovalDecision.ALLOW_ONCE,),
        ),
        created_at=now,
    )
    requested = request_interaction(running.state, interaction, now=now)
    await persistence.commit_interaction_request(
        state.conversation.id,
        state.conversation.version,
        requested.state,
        (*queued.events, *running.events, *requested.events),
        interaction_id=interaction.id,
        request_event_sequence=requested.events[-1].sequence,
    )
    allow = submit_interaction_answer(
        requested.state,
        InteractionAnswer(interaction_id=interaction.id, decision=ApprovalDecision.ALLOW_ONCE),
        now=now,
    )
    rule_a = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "x")),
        created_at=now,
        updated_at=now,
    )
    rule_b = ApprovalRule(
        principal_id="owner",
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("tool", "x")),
        created_at=now,
        updated_at=now,
    )
    first = await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        allow.state,
        allow.events,
        allow.state.answers[interaction.id],
        create_rule=rule_a,
        deciding_rule=rule_a,
        resolution_event_sequence=allow.events[-1].sequence,
    )
    second = await persistence.commit_interaction_resolution(
        state.conversation.id,
        "owner",
        requested.state.conversation.version,
        allow.state,
        allow.events,
        allow.state.answers[interaction.id],
        create_rule=rule_b,
        deciding_rule=rule_b,
        resolution_event_sequence=allow.events[-1].sequence,
    )
    assert first.was_first_write is True
    assert second.was_first_write is False
    rules = await persistence.list_applicable_approval_rules("owner")
    assert {r.id for r in rules} == {rule_a.id}


def _bound_state(owner_id: str, now: datetime) -> ConversationState:
    conversation_id = uuid4()
    binding = ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.OPENCODE,
        configuration=HarnessConfiguration(
            kind=HarnessKind.OPENCODE,
            working_directory="/tmp",
        ),
        created_at=now,
    )
    return new_conversation_state(
        owner_id=owner_id,
        now=now,
        binding=binding,
        conversation_id=conversation_id,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_claim_expired_conversations_and_lease_edges() -> None:
    from talktoharnesses.django.models import ConversationAggregate, RecoveryAttemptRecord
    from talktoharnesses.domain.enums import ConversationStatus, RecoveryTrigger

    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    # Unowned active conversation is claimable.
    active = _bound_state("owner", now)
    assert active.binding is not None
    active = active.model_copy(
        update={
            "conversation": active.conversation.model_copy(
                update={"status": ConversationStatus.RUNNING}
            )
        }
    )
    await persistence.save_snapshot(active)

    # Expired owned conversation is claimable and abandons open attempts.
    expired = _bound_state("owner-2", now)
    assert expired.binding is not None
    await persistence.save_snapshot(expired)
    await ConversationAggregate.objects.filter(conversation_id=expired.conversation.id).aupdate(
        runtime_worker_id="dead",
        runtime_fence=2,
        runtime_lease_expires_at=now - timedelta(seconds=5),
        status=ConversationStatus.WAITING.value,
    )
    await RecoveryAttemptRecord.objects.acreate(
        attempt_id=uuid4(),
        conversation_id=expired.conversation.id,
        binding_id=expired.binding.id,
        worker_id="dead",
        fence=2,
        trigger=RecoveryTrigger.STARTUP.value,
        observed_delivery_phase="none",
        action="no_action",
        result=None,
        reason_code="worker_lost",
        started_at=now - timedelta(minutes=1),
    )

    claimed = await persistence.claim_expired_conversations(
        "worker-live",
        10,
        lease_duration=30.0,
        trigger=RecoveryTrigger.TAKEOVER.value,
    )
    claimed_ids = {item.conversation_id for item in claimed}
    assert active.conversation.id in claimed_ids
    assert expired.conversation.id in claimed_ids
    expired_claim = next(
        item for item in claimed if item.conversation_id == expired.conversation.id
    )
    assert expired_claim.fence == 3
    abandoned = await RecoveryAttemptRecord.objects.filter(
        conversation_id=expired.conversation.id,
        result="abandoned",
    ).acount()
    assert abandoned == 1

    # Live owner renews; foreign expired row is reported lost.
    lost = await persistence.renew_owned_conversation_leases("worker-live", lease_duration=30.0)
    assert lost == ()
    await ConversationAggregate.objects.filter(conversation_id=active.conversation.id).aupdate(
        runtime_lease_expires_at=now - timedelta(seconds=1)
    )
    lost = await persistence.renew_owned_conversation_leases("worker-live", lease_duration=30.0)
    assert any(item.conversation_id == active.conversation.id for item in lost)

    # Re-claim then release under fence.
    reclaimed = await persistence.claim_expired_conversations(
        "worker-live",
        1,
        lease_duration=30.0,
    )
    assert len(reclaimed) == 1
    await persistence.release_conversation_lease(
        reclaimed[0].conversation_id,
        "worker-live",
        reclaimed[0].fence,
    )
    row = await ConversationAggregate.objects.aget(conversation_id=reclaimed[0].conversation_id)
    assert row.runtime_worker_id is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fenced_session_rotation_and_stale_owner() -> None:
    from talktoharnesses.django.models import ConversationAggregate

    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    state = _bound_state("owner", now)
    assert state.binding is not None
    await persistence.save_snapshot(state)
    await ConversationAggregate.objects.filter(conversation_id=state.conversation.id).aupdate(
        runtime_worker_id="worker-a",
        runtime_fence=4,
        runtime_lease_expires_at=now + timedelta(hours=1),
    )
    launch = LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1",
        working_directory="/tmp",
        adapter_version="1",
        capabilities=HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1"),
    )
    await persistence.commit_session_rotation(
        state.conversation.id,
        state.conversation.version,
        native_session_id="rotated-native",
        launch_snapshot=launch,
        worker_id="worker-a",
        fence=4,
    )
    rotated = await persistence.get_snapshot(state.conversation.id, "owner")
    assert rotated.binding is not None
    assert rotated.binding.native_session_id == "rotated-native"
    assert rotated.binding.requires_session_recreation is False

    with pytest.raises(DomainError) as stale:
        await persistence.commit_rotation_requires_recreation(
            state.conversation.id,
            rotated.conversation.version,
            worker_id="worker-b",
            fence=4,
        )
    assert stale.value.code is ErrorCode.STALE_OWNER

    await persistence.commit_rotation_requires_recreation(
        state.conversation.id,
        rotated.conversation.version,
        worker_id="worker-a",
        fence=4,
    )
    marked = await persistence.get_snapshot(state.conversation.id, "owner")
    assert marked.binding is not None
    assert marked.binding.requires_session_recreation is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_commit_transcript_import_and_export_edges() -> None:
    from talktoharnesses.application.handoff import HandoffDocument, HandoffMessage, HandoffTool
    from talktoharnesses.django.models import MessageRecord, ToolRecord, TurnRecord
    from talktoharnesses.domain.enums import MessageRole, ToolOutcome
    from talktoharnesses.domain.events import ConversationEvent, SessionStartedPayload

    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    state = _bound_state("owner", now)
    assert state.binding is not None
    turn_id = uuid4()
    user_id = uuid4()
    assistant_id = uuid4()
    tool_id = uuid4()
    handoff = HandoffDocument(
        entries=(
            HandoffMessage(
                id=user_id,
                turn_id=turn_id,
                role=MessageRole.USER,
                text="hello",
                turn_order_index=1,
                order_index=1,
            ),
            HandoffTool(
                id=tool_id,
                turn_id=turn_id,
                tool_name="shell",
                arguments={"cmd": "echo"},
                outcome=ToolOutcome.SUCCESS,
                exit_status=0,
                paths=("/tmp",),
                output_tail="ok",
                turn_order_index=1,
                order_index=2,
            ),
            HandoffMessage(
                id=assistant_id,
                turn_id=turn_id,
                role=MessageRole.ASSISTANT,
                text="done",
                turn_order_index=1,
                order_index=3,
            ),
        )
    )
    events = (
        ConversationEvent(
            conversation_id=state.conversation.id,
            sequence=1,
            timestamp=now,
            type="session_started",
            payload=SessionStartedPayload(
                binding_id=state.binding.id,
                native_session_id="imported",
                harness_kind=HarnessKind.OPENCODE,
            ),
        ),
    )
    imported_state = state.model_copy(
        update={
            "conversation": state.conversation.model_copy(update={"next_event_sequence": 2}),
            "binding": state.binding.model_copy(update={"native_session_id": "imported"}),
        }
    )
    process = ProcessRecord(
        conversation_id=state.conversation.id,
        binding_id=state.binding.id,
        status=ProcessStatus.RUNNING,
        pid=9,
        started_at=now,
    )
    launch = LaunchSnapshot(
        resolved_executable="/bin/true",
        harness_version="1",
        working_directory="/tmp",
        adapter_version="1",
        capabilities=HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1"),
    )
    committed = await persistence.commit_transcript_import(
        imported_state,
        handoff,
        events,
        process=process,
        launch_history_entry=launch,
    )
    assert committed == events
    assert await TurnRecord.objects.filter(conversation_id=state.conversation.id).acount() == 1
    assert await MessageRecord.objects.filter(conversation_id=state.conversation.id).acount() == 2
    assert await ToolRecord.objects.filter(conversation_id=state.conversation.id).acount() == 1

    handoff_doc, display_title = await persistence.read_retained_export(
        state.conversation.id, "owner"
    )
    assert isinstance(display_title, str)
    assert len(handoff_doc.entries) >= 1

    with pytest.raises(DomainError) as exists:
        await persistence.commit_transcript_import(imported_state, handoff, events)
    assert exists.value.code is ErrorCode.INVALID_STATE

    with pytest.raises(DomainError) as launch_only:
        fresh = _bound_state("owner-b", now)
        fresh = fresh.model_copy(
            update={
                "conversation": fresh.conversation.model_copy(update={"next_event_sequence": 1})
            }
        )
        await persistence.commit_transcript_import(
            fresh,
            HandoffDocument(),
            (),
            launch_history_entry=launch,
        )
    assert launch_only.value.code is ErrorCode.INVALID_STATE


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_commit_recovery_batch_and_complete_attempt() -> None:
    from talktoharnesses.django.models import ConversationAggregate, RecoveryAttemptRecord
    from talktoharnesses.domain.enums import (
        ConversationStatus,
        ObservedDeliveryPhase,
        RecoveryAction,
        RecoveryReasonCode,
        RecoveryResultCode,
        RecoveryTrigger,
        TurnStatus,
    )
    from talktoharnesses.domain.events import ConversationEvent, TurnOutcomeUnknownPayload
    from talktoharnesses.domain.models import Turn

    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    state = _bound_state("owner", now)
    assert state.binding is not None
    turn = Turn(
        conversation_id=state.conversation.id,
        status=TurnStatus.RUNNING,
        created_at=now,
        started_at=now,
    )
    command = Command(
        conversation_id=state.conversation.id,
        kind=CommandKind.SUBMIT_TURN,
        status=CommandStatus.DELIVERY_STARTED,
        idempotency_key="rec-1",
        target_turn_id=turn.id,
        payload=SubmitTurnPayload(prompt="hi"),
        created_at=now,
        delivery_started_at=now,
    )
    state = state.model_copy(
        update={
            "active_turn": turn,
            "commands": {command.id: command},
            "conversation": state.conversation.model_copy(
                update={
                    "status": ConversationStatus.RUNNING,
                    "active_turn_id": turn.id,
                    "next_event_sequence": 1,
                }
            ),
        }
    )
    await persistence.save_snapshot(state)
    await ConversationAggregate.objects.filter(conversation_id=state.conversation.id).aupdate(
        runtime_worker_id="worker-a",
        runtime_fence=1,
        runtime_lease_expires_at=now + timedelta(hours=1),
    )
    attempt_id = uuid4()
    recovery_binding = state.binding
    assert recovery_binding is not None
    await RecoveryAttemptRecord.objects.acreate(
        attempt_id=attempt_id,
        conversation_id=state.conversation.id,
        binding_id=recovery_binding.id,
        worker_id="worker-a",
        fence=1,
        trigger=RecoveryTrigger.TAKEOVER.value,
        observed_delivery_phase=ObservedDeliveryPhase.DELIVERY_STARTED.value,
        action=RecoveryAction.OUTCOME_UNKNOWN.value,
        result=None,
        reason_code=RecoveryReasonCode.DELIVERY_AMBIGUOUS.value,
        started_at=now,
    )
    next_state = state.model_copy(
        update={
            "active_turn": None,
            "commands": {
                command.id: command.model_copy(update={"status": CommandStatus.OUTCOME_UNKNOWN})
            },
            "conversation": state.conversation.model_copy(
                update={
                    "status": ConversationStatus.IDLE,
                    "active_turn_id": None,
                    "version": state.conversation.version,
                    "next_event_sequence": 2,
                }
            ),
        }
    )
    events = (
        ConversationEvent(
            conversation_id=state.conversation.id,
            sequence=1,
            timestamp=now,
            type="turn_outcome_unknown",
            payload=TurnOutcomeUnknownPayload(turn_id=turn.id, message="ambiguous"),
        ),
    )
    committed = await persistence.commit_recovery_batch(
        state.conversation.id,
        state.conversation.version,
        next_state,
        events,
        (next_state.commands[command.id],),
        interrupted_turn_id=turn.id,
        attempt_id=attempt_id,
        command_id=command.id,
        turn_id=turn.id,
        trigger=RecoveryTrigger.TAKEOVER.value,
        observed_delivery_phase=ObservedDeliveryPhase.DELIVERY_STARTED.value,
        action=RecoveryAction.OUTCOME_UNKNOWN.value,
        result=RecoveryResultCode.SUCCESS.value,
        reason_code=RecoveryReasonCode.DELIVERY_AMBIGUOUS.value,
        completed_at=now,
        worker_id="worker-a",
        fence=1,
    )
    assert committed
    attempt = await RecoveryAttemptRecord.objects.aget(attempt_id=attempt_id)
    assert attempt.result == RecoveryResultCode.SUCCESS.value

    with pytest.raises(DomainError) as done:
        await persistence.complete_recovery_attempt(
            attempt_id,
            result=RecoveryResultCode.FAILED.value,
            reason_code=RecoveryReasonCode.WORKER_LOST.value,
            completed_at=now,
        )
    assert done.value.code is ErrorCode.INVALID_STATE


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_retention_preview_and_cleanup_listings() -> None:
    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    state = _bound_state("owner-ret", now)
    await persistence.save_snapshot(state)
    await persistence.replace_retention_policy("owner-ret", 1, now=now)
    preview = await persistence.preview_retention("owner-ret", now=now)
    assert preview.cutoff is not None
    owners = await persistence.list_retention_owner_ids()
    assert "owner-ret" in owners
    cleanup = await persistence.list_cleanup_conversation_ids()
    assert any(cid == state.conversation.id for cid, _ in cleanup)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_update_and_get_open_recovery_attempt() -> None:
    from talktoharnesses.django.models import ConversationAggregate, RecoveryAttemptRecord
    from talktoharnesses.domain.enums import (
        ObservedDeliveryPhase,
        RecoveryAction,
        RecoveryReasonCode,
        RecoveryTrigger,
    )

    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    state = _bound_state("owner", now)
    assert state.binding is not None
    await persistence.save_snapshot(state)
    await ConversationAggregate.objects.filter(conversation_id=state.conversation.id).aupdate(
        runtime_worker_id="worker-a",
        runtime_fence=2,
        runtime_lease_expires_at=now + timedelta(hours=1),
    )
    attempt_id = uuid4()
    await RecoveryAttemptRecord.objects.acreate(
        attempt_id=attempt_id,
        conversation_id=state.conversation.id,
        binding_id=state.binding.id,
        worker_id="worker-a",
        fence=2,
        trigger=RecoveryTrigger.STARTUP.value,
        observed_delivery_phase=ObservedDeliveryPhase.NONE.value,
        action=RecoveryAction.NO_ACTION.value,
        result=None,
        reason_code=RecoveryReasonCode.NO_ACTION.value,
        started_at=now,
    )
    open_attempt = await persistence.get_open_recovery_attempt(state.conversation.id, "worker-a", 2)
    assert open_attempt is not None
    assert open_attempt.id == attempt_id
    command_id = uuid4()
    turn_id = uuid4()
    await persistence.update_recovery_attempt(
        attempt_id,
        command_id=command_id,
        turn_id=turn_id,
        trigger=RecoveryTrigger.TAKEOVER.value,
        observed_delivery_phase=ObservedDeliveryPhase.DELIVERED.value,
        action=RecoveryAction.NATIVE_RESUME.value,
        reason_code=RecoveryReasonCode.UNCHANGED_LAUNCH.value,
        worker_id="worker-a",
        fence=2,
    )
    updated = await RecoveryAttemptRecord.objects.aget(attempt_id=attempt_id)
    assert updated.command_id == command_id
    assert updated.action == RecoveryAction.NATIVE_RESUME.value


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_suppressed_resolution_paging_purge_and_worker_lease() -> None:
    from talktoharnesses.django.models import (
        ActivityRecord,
        ConversationAggregate,
        InteractionAnswerRecord,
        InteractionRecord,
        MessageRecord,
        PlanRecord,
        ToolRecord,
        TurnRecord,
    )
    from talktoharnesses.domain.enums import (
        InteractionStatus,
        MessageRole,
        ToolOutcome,
        TurnStatus,
    )

    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    state = _bound_state("owner-page", now)
    assert state.binding is not None
    await persistence.save_snapshot(state)
    cid = state.conversation.id

    turn_ids = [uuid4() for _ in range(3)]
    for index, turn_id in enumerate(turn_ids):
        await TurnRecord.objects.acreate(
            turn_id=turn_id,
            conversation_id=cid,
            status=TurnStatus.COMPLETED.value,
            created_at=now,
            started_at=now,
            completed_at=now,
            order_index=index + 1,
        )
        await MessageRecord.objects.acreate(
            message_id=uuid4(),
            conversation_id=cid,
            turn_id=turn_id,
            role=MessageRole.USER.value,
            text=f"m{index}",
            sequence=0,
            interrupted=False,
            completed=True,
            created_at=now + timedelta(seconds=index),
            order_index=index + 1,
        )
        await ToolRecord.objects.acreate(
            tool_id=uuid4(),
            conversation_id=cid,
            turn_id=turn_id,
            tool_name="shell",
            arguments={},
            outcome=ToolOutcome.SUCCESS.value,
            order_index=index + 1,
        )
        await PlanRecord.objects.acreate(
            plan_id=uuid4(),
            conversation_id=cid,
            turn_id=turn_id,
            items=[],
            order_index=index + 1,
        )
        await ActivityRecord.objects.acreate(
            activity_id=uuid4(),
            conversation_id=cid,
            parent_turn_id=turn_id,
            status="completed",
            title=f"act-{index}",
            created_at=now + timedelta(seconds=index),
        )

    interaction_ids = [uuid4() for _ in range(3)]
    for index, interaction_id in enumerate(interaction_ids):
        await InteractionRecord.objects.acreate(
            interaction_id=interaction_id,
            conversation_id=cid,
            turn_id=turn_ids[0],
            kind=InteractionKind.APPROVAL.value,
            status=InteractionStatus.PENDING.value,
            request={"tool_name": "shell", "available_decisions": ["allow_once", "deny", "cancel"]},
            created_at=now + timedelta(seconds=index),
        )

    turns = await persistence.page_turns(cid, "owner-page", limit=2)
    assert len(turns.items) == 2
    assert turns.next_cursor is not None
    turns2 = await persistence.page_turns(cid, "owner-page", cursor=turns.next_cursor, limit=2)
    assert len(turns2.items) == 1

    messages = await persistence.page_messages(cid, "owner-page", limit=2)
    assert len(messages.items) == 2 and messages.next_cursor
    tools = await persistence.page_tools(cid, "owner-page", limit=2)
    assert len(tools.items) == 2 and tools.next_cursor
    plans = await persistence.page_plans(cid, "owner-page", limit=2)
    assert len(plans.items) == 2 and plans.next_cursor
    activity = await persistence.page_activity(cid, "owner-page", limit=2)
    assert len(activity.items) == 2 and activity.next_cursor
    pending = await persistence.page_pending_interactions(cid, "owner-page", limit=2)
    assert len(pending.items) == 2 and pending.next_cursor

    # Suppressed interaction resolution completion.
    suppressed_id = interaction_ids[0]
    await InteractionAnswerRecord.objects.acreate(
        interaction_id=suppressed_id,
        conversation_id=cid,
        data={"decision": "allow_once"},
        answer_command_suppressed=True,
        released_at=None,
        submitted_at=now,
    )
    assert await persistence.complete_suppressed_interaction_resolution(suppressed_id, now) is True
    assert await persistence.complete_suppressed_interaction_resolution(suppressed_id, now) is True
    unsuppressed = uuid4()
    await InteractionAnswerRecord.objects.acreate(
        interaction_id=unsuppressed,
        conversation_id=cid,
        data={"decision": "deny"},
        answer_command_suppressed=False,
        released_at=None,
        submitted_at=now,
    )
    assert await persistence.complete_suppressed_interaction_resolution(unsuppressed, now) is False
    with pytest.raises(DomainError):
        await persistence.complete_suppressed_interaction_resolution(uuid4(), now)

    await persistence.mark_interaction_policy_evaluated(suppressed_id, now)
    uneval = await persistence.list_unevaluated_open_interactions()
    assert all(item[1] != suppressed_id for item in uneval)

    # Soft-delete purge.
    await ConversationAggregate.objects.filter(conversation_id=cid).aupdate(
        deleted_at=now - timedelta(days=400)
    )
    await persistence.replace_retention_policy("owner-page", 1, now=now)
    purged = await persistence.purge_soft_deleted(now)
    assert purged >= 1

    # Worker lease acquire/renew/drain/release on sqlite singleton slot.
    await persistence.acquire_worker_lease("worker-z", lease_duration=30.0)
    await persistence.acquire_worker_lease("worker-z", lease_duration=30.0)  # renew same
    await persistence.renew_worker_lease("worker-z", lease_duration=30.0)
    await persistence.mark_worker_draining("worker-z")
    await persistence.release_worker_lease("worker-z")
    with pytest.raises(DomainError):
        await persistence.renew_worker_lease("worker-z", lease_duration=30.0)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_interaction_event_lookup_harness_probe_and_search_phrase() -> None:
    from talktoharnesses.django.models import ConversationEventRecord, InteractionRecord
    from talktoharnesses.domain.enums import InteractionStatus
    from talktoharnesses.domain.events import ConversationEvent, InteractionRequestedPayload
    from talktoharnesses.domain.models import HarnessInstance

    now = datetime.now(UTC)
    persistence = DjangoPersistence()
    state = _bound_state("owner-evt", now)
    await persistence.save_snapshot(state)
    cid = state.conversation.id
    interaction_id = uuid4()

    with pytest.raises(DomainError):
        await persistence.get_interaction_request_event(cid, interaction_id)

    await InteractionRecord.objects.acreate(
        interaction_id=interaction_id,
        conversation_id=cid,
        turn_id=uuid4(),
        kind=InteractionKind.APPROVAL.value,
        status=InteractionStatus.PENDING.value,
        request={"tool_name": "shell", "available_decisions": ["cancel"]},
        created_at=now,
        request_event_sequence=1,
    )
    with pytest.raises(DomainError):
        await persistence.get_interaction_request_event(cid, interaction_id)

    event = ConversationEvent(
        conversation_id=cid,
        sequence=1,
        timestamp=now,
        type="interaction_requested",
        payload=InteractionRequestedPayload(
            turn_id=uuid4(),
            interaction_id=interaction_id,
            kind=InteractionKind.APPROVAL,
            request=ApprovalRequestPayload(tool_name="shell"),
        ),
    )
    await ConversationEventRecord.objects.acreate(
        event_id=uuid4(),
        conversation_id=cid,
        sequence=1,
        timestamp=now,
        type=event.type,
        payload=event.model_dump(mode="json"),
    )
    loaded = await persistence.get_interaction_request_event(cid, interaction_id)
    assert loaded.payload.interaction_id == interaction_id  # type: ignore[attr-defined]

    harness = await persistence.create_harness(
        HarnessInstance(
            owner_id="owner-evt",
            name="probe-me",
            kind=HarnessKind.OPENCODE,
            configuration=HarnessConfiguration(kind=HarnessKind.OPENCODE, working_directory="/tmp"),
            created_at=now,
        )
    )
    caps = HarnessCapabilities(kind=HarnessKind.OPENCODE, version="1.2.27")
    probe = await persistence.save_harness_probe(harness.id, "owner-evt", caps, probed_at=now)
    assert probe.capabilities.version == "1.2.27"
    got = await persistence.get_harness_probe(harness.id, "owner-evt")
    assert got.harness_id == probe.harness_id
    listed = await persistence.list_harnesses("owner-evt", limit=10)
    assert any(item.id == harness.id for item in listed.items)
    assert await persistence.has_fresh_harness_probe(now=now, max_age_seconds=3600) is True
    assert (
        await persistence.has_fresh_harness_probe(now=now + timedelta(hours=2), max_age_seconds=1)
        is False
    )

    from talktoharnesses.django.materialize import materialize_projections
    from talktoharnesses.django.models import MessageRecord, TurnRecord
    from talktoharnesses.domain.enums import MessageRole, TurnStatus

    turn_id = uuid4()
    await TurnRecord.objects.acreate(
        turn_id=turn_id,
        conversation_id=cid,
        status=TurnStatus.COMPLETED.value,
        created_at=now,
        started_at=now,
        completed_at=now,
        order_index=1,
    )
    await MessageRecord.objects.acreate(
        message_id=uuid4(),
        conversation_id=cid,
        turn_id=turn_id,
        role=MessageRole.USER.value,
        text='find this "exact phrase" needle',
        sequence=0,
        interrupted=False,
        completed=True,
        created_at=now,
        order_index=1,
    )
    from asgiref.sync import sync_to_async

    await sync_to_async(materialize_projections, thread_sensitive=True)(state, ())
    hits = await persistence.search_conversations("owner-evt", '"exact phrase"', limit=10)
    assert isinstance(hits.items, tuple)
