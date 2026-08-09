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
