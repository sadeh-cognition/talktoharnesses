"""Project ConversationState + committed events into durable projection rows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from talktoharnesses.application.titles import derive_title_from_user_message
from talktoharnesses.django.models import (
    ActivityRecord,
    ConversationAggregate,
    ConversationBindingRecord,
    InteractionRecord,
    MessageRecord,
    PlanRecord,
    ReasoningRecord,
    SearchDocument,
    ToolRecord,
    TurnRecord,
    UsageRecordRow,
)
from talktoharnesses.domain.enums import InteractionStatus, MessageRole, ToolOutcome, TurnStatus
from talktoharnesses.domain.events import (
    ActivityCompletedPayload,
    ActivityStartedPayload,
    AssistantMessageCompletedPayload,
    AssistantMessageDeltaPayload,
    AssistantMessageStartedPayload,
    ConversationEvent,
    CostUpdatedPayload,
    InteractionDraftUpdatedPayload,
    InteractionRequestedPayload,
    InteractionResolvedPayload,
    PlanCreatedPayload,
    PlanUpdatedPayload,
    ReasoningCompletedPayload,
    ReasoningDeltaPayload,
    ReasoningStartedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolOutputDeltaPayload,
    ToolRequestedPayload,
    ToolStartedPayload,
    TurnCancelledPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
    TurnInterruptedPayload,
    TurnOutcomeUnknownPayload,
    TurnQueuedPayload,
    TurnStartedPayload,
    UsageUpdatedPayload,
)
from talktoharnesses.domain.models import Turn, limit_tool_output_tail
from talktoharnesses.domain.transitions import ConversationState


def materialize_projections(
    state: ConversationState,
    events: Sequence[ConversationEvent],
) -> None:
    """Upsert denormalized shell fields and history rows for one commit."""
    conversation = state.conversation
    cid = conversation.id
    try:
        ConversationAggregate.objects.get(conversation_id=cid)
    except ConversationAggregate.DoesNotExist:
        return

    binding = state.binding
    pending = any(
        i.status in {InteractionStatus.PENDING, InteractionStatus.DRAFT}
        for i in state.interactions.values()
    )
    ConversationAggregate.objects.filter(conversation_id=cid).update(
        title=conversation.display_title,
        status=conversation.status.value,
        harness_kind=binding.kind.value if binding else None,
        model=binding.configuration.model if binding else None,
        mode=binding.configuration.mode if binding else None,
        has_pending_interactions=pending,
        pinned_at=conversation.pinned_at,
        archived_at=conversation.archived_at,
        snoozed_until=conversation.snoozed_until,
        latest_activity_at=conversation.updated_at,
    )

    if state.active_turn is not None:
        _upsert_turn(cid, state.active_turn)
    if state.queued_turn is not None:
        _upsert_turn(cid, state.queued_turn)

    for interaction in state.interactions.values():
        InteractionRecord.objects.update_or_create(
            interaction_id=interaction.id,
            defaults={
                "conversation_id": cid,
                "turn_id": interaction.turn_id,
                "kind": interaction.kind.value,
                "status": interaction.status.value,
                "request": interaction.request.model_dump(mode="json"),
                "draft": interaction.draft,
                "created_at": interaction.created_at,
            },
        )

    for activity in state.activities.values():
        ActivityRecord.objects.update_or_create(
            activity_id=activity.id,
            defaults={
                "conversation_id": cid,
                "parent_turn_id": activity.parent_turn_id,
                "parent_activity_id": activity.parent_activity_id,
                "status": activity.status.value,
                "title": activity.title,
                "summary": activity.summary,
                "created_at": activity.created_at,
                "completed_at": activity.completed_at,
            },
        )

    for event in events:
        _apply_event(cid, event)

    sync_active_binding(state)
    _refresh_search_document(_recompute_derived_title(state))


def _next_order_index(conversation_id: UUID) -> int:
    last = (
        TurnRecord.objects.filter(conversation_id=conversation_id)
        .order_by("-order_index")
        .values_list("order_index", flat=True)
        .first()
    )
    return int(last or 0) + 1


def _upsert_turn(conversation_id: UUID, turn: Turn) -> None:
    existing = TurnRecord.objects.filter(turn_id=turn.id).first()
    if existing is not None:
        order_index = existing.order_index
        user_message_id = turn.user_message_id or existing.user_message_id
    else:
        order_index = _next_order_index(conversation_id)
        user_message_id = turn.user_message_id
    TurnRecord.objects.update_or_create(
        turn_id=turn.id,
        defaults={
            "conversation_id": conversation_id,
            "status": turn.status.value,
            "user_message_id": user_message_id,
            "command_id": turn.command_id,
            "terminal_reason": turn.terminal_reason,
            "created_at": turn.created_at,
            "started_at": turn.started_at,
            "completed_at": turn.completed_at,
            "order_index": order_index,
        },
    )


def _message_order_index(message_id: UUID, event_sequence: int) -> int:
    """Conversation-wide message order from the event that first created it."""
    existing = (
        MessageRecord.objects.filter(message_id=message_id)
        .values_list("order_index", flat=True)
        .first()
    )
    return int(existing) if existing else event_sequence


def _ensure_turn(conversation_id: UUID, turn_id: UUID, *, at: datetime) -> None:
    if TurnRecord.objects.filter(turn_id=turn_id).exists():
        return
    TurnRecord.objects.create(
        turn_id=turn_id,
        conversation_id=conversation_id,
        status=TurnStatus.RUNNING.value,
        created_at=at,
        order_index=_next_order_index(conversation_id),
    )


def _apply_event(conversation_id: UUID, event: ConversationEvent) -> None:
    payload = event.payload
    ts = event.timestamp

    if isinstance(payload, TurnQueuedPayload):
        turn = Turn(
            id=payload.turn_id,
            conversation_id=conversation_id,
            status=TurnStatus.QUEUED,
            command_id=payload.command_id,
            created_at=ts,
        )
        _upsert_turn(conversation_id, turn)
        if payload.prompt:
            existing = TurnRecord.objects.filter(turn_id=payload.turn_id).first()
            msg_id = existing.user_message_id if existing and existing.user_message_id else uuid4()
            MessageRecord.objects.update_or_create(
                message_id=msg_id,
                defaults={
                    "conversation_id": conversation_id,
                    "turn_id": payload.turn_id,
                    "role": MessageRole.USER.value,
                    "text": payload.prompt,
                    "sequence": 0,
                    "created_at": ts,
                    "order_index": _message_order_index(msg_id, event.sequence),
                },
            )
            TurnRecord.objects.filter(turn_id=payload.turn_id).update(user_message_id=msg_id)
        return

    if isinstance(payload, TurnStartedPayload):
        _ensure_turn(conversation_id, payload.turn_id, at=ts)
        TurnRecord.objects.filter(turn_id=payload.turn_id).update(
            status=TurnStatus.RUNNING.value,
            started_at=ts,
            command_id=payload.command_id,
        )
        return

    if isinstance(payload, TurnCompletedPayload):
        TurnRecord.objects.filter(turn_id=payload.turn_id).update(
            status=TurnStatus.COMPLETED.value,
            completed_at=ts,
            terminal_reason=payload.terminal_reason,
        )
        return

    if isinstance(payload, TurnInterruptedPayload):
        TurnRecord.objects.filter(turn_id=payload.turn_id).update(
            status=TurnStatus.INTERRUPTED.value,
            completed_at=ts,
            terminal_reason=payload.reason,
        )
        return

    if isinstance(payload, TurnFailedPayload):
        TurnRecord.objects.filter(turn_id=payload.turn_id).update(
            status=TurnStatus.FAILED.value,
            completed_at=ts,
            terminal_reason=payload.message,
        )
        return

    if isinstance(payload, TurnOutcomeUnknownPayload):
        TurnRecord.objects.filter(turn_id=payload.turn_id).update(
            status=TurnStatus.OUTCOME_UNKNOWN.value,
            completed_at=ts,
            terminal_reason=payload.message,
        )
        return

    if isinstance(payload, TurnCancelledPayload):
        TurnRecord.objects.filter(turn_id=payload.turn_id).update(
            status=TurnStatus.INTERRUPTED.value,
            completed_at=ts,
            terminal_reason="cancelled",
        )
        return

    if isinstance(payload, AssistantMessageStartedPayload):
        _ensure_turn(conversation_id, payload.turn_id, at=ts)
        MessageRecord.objects.update_or_create(
            message_id=payload.message_id,
            defaults={
                "conversation_id": conversation_id,
                "turn_id": payload.turn_id,
                "role": MessageRole.ASSISTANT.value,
                "text": "",
                "sequence": 0,
                "completed": False,
                "created_at": ts,
                "order_index": _message_order_index(payload.message_id, event.sequence),
            },
        )
        return

    if isinstance(payload, AssistantMessageDeltaPayload):
        row = MessageRecord.objects.filter(message_id=payload.message_id).first()
        if row is not None:
            row.text = row.text + payload.text
            row.sequence = payload.sequence
            row.save(update_fields=("text", "sequence"))
        return

    if isinstance(payload, AssistantMessageCompletedPayload):
        MessageRecord.objects.filter(message_id=payload.message_id).update(
            text=payload.text,
            completed=True,
        )
        return

    if isinstance(payload, ReasoningStartedPayload):
        _ensure_turn(conversation_id, payload.turn_id, at=ts)
        ReasoningRecord.objects.update_or_create(
            reasoning_id=payload.reasoning_id,
            defaults={
                "conversation_id": conversation_id,
                "turn_id": payload.turn_id,
                "text": "",
                "completed": False,
            },
        )
        return

    if isinstance(payload, ReasoningDeltaPayload):
        row = ReasoningRecord.objects.filter(reasoning_id=payload.reasoning_id).first()
        if row is not None:
            row.text = row.text + payload.text
            row.save(update_fields=("text",))
        return

    if isinstance(payload, ReasoningCompletedPayload):
        ReasoningRecord.objects.filter(reasoning_id=payload.reasoning_id).update(
            text=payload.text,
            completed=True,
        )
        return

    if isinstance(payload, (PlanCreatedPayload, PlanUpdatedPayload)):
        _ensure_turn(conversation_id, payload.turn_id, at=ts)
        items = [item.model_dump(mode="json") for item in payload.items]
        existing = PlanRecord.objects.filter(plan_id=payload.plan_id).first()
        PlanRecord.objects.update_or_create(
            plan_id=payload.plan_id,
            defaults={
                "conversation_id": conversation_id,
                "turn_id": payload.turn_id,
                "items": items,
                "order_index": existing.order_index if existing else event.sequence,
            },
        )
        return

    if isinstance(payload, ToolRequestedPayload):
        _ensure_turn(conversation_id, payload.turn_id, at=ts)
        existing = ToolRecord.objects.filter(tool_id=payload.tool_id).first()
        ToolRecord.objects.update_or_create(
            tool_id=payload.tool_id,
            defaults={
                "conversation_id": conversation_id,
                "turn_id": payload.turn_id,
                "tool_name": payload.tool_name,
                "arguments": dict(payload.arguments),
                "outcome": ToolOutcome.UNKNOWN.value,
                "order_index": existing.order_index if existing else event.sequence,
            },
        )
        return

    if isinstance(payload, ToolStartedPayload):
        ToolRecord.objects.filter(tool_id=payload.tool_id).update(tool_name=payload.tool_name)
        return

    if isinstance(payload, ToolOutputDeltaPayload):
        row = ToolRecord.objects.filter(tool_id=payload.tool_id).first()
        if row is not None:
            full = (row.full_output or "") + payload.text
            row.full_output = full
            row.output_tail = limit_tool_output_tail(full)
            row.save(update_fields=("full_output", "output_tail"))
        return

    if isinstance(payload, ToolCompletedPayload):
        ToolRecord.objects.filter(tool_id=payload.tool_id).update(
            tool_name=payload.tool_name,
            outcome=payload.outcome.value,
            exit_status=payload.exit_status,
            output_tail=payload.output_tail,
        )
        return

    if isinstance(payload, ToolFailedPayload):
        ToolRecord.objects.filter(tool_id=payload.tool_id).update(
            tool_name=payload.tool_name,
            outcome=ToolOutcome.FAILURE.value,
            output_tail=payload.message,
        )
        return

    if isinstance(payload, InteractionRequestedPayload):
        InteractionRecord.objects.update_or_create(
            interaction_id=payload.interaction_id,
            defaults={
                "conversation_id": conversation_id,
                "turn_id": payload.turn_id,
                "kind": payload.kind.value,
                "status": InteractionStatus.PENDING.value,
                "request": payload.request.model_dump(mode="json"),
                "draft": None,
                "created_at": ts,
            },
        )
        return

    if isinstance(payload, InteractionDraftUpdatedPayload):
        InteractionRecord.objects.filter(interaction_id=payload.interaction_id).update(
            status=InteractionStatus.DRAFT.value,
            draft=payload.draft,
        )
        return

    if isinstance(payload, InteractionResolvedPayload):
        from talktoharnesses.domain.enums import ApprovalDecision

        status = (
            InteractionStatus.CANCELLED
            if payload.decision is ApprovalDecision.CANCEL
            else InteractionStatus.RESOLVED
        )
        InteractionRecord.objects.filter(interaction_id=payload.interaction_id).update(
            status=status.value
        )
        return

    if isinstance(payload, ActivityStartedPayload):
        ActivityRecord.objects.update_or_create(
            activity_id=payload.activity_id,
            defaults={
                "conversation_id": conversation_id,
                "parent_turn_id": payload.parent_turn_id,
                "parent_activity_id": payload.parent_activity_id,
                "status": "running",
                "title": payload.title,
                "summary": None,
                "created_at": ts,
                "completed_at": None,
            },
        )
        return

    if isinstance(payload, ActivityCompletedPayload):
        ActivityRecord.objects.filter(activity_id=payload.activity_id).update(
            status=payload.status.value,
            summary=payload.summary,
            completed_at=ts,
        )
        return

    if isinstance(payload, UsageUpdatedPayload) and payload.turn_id is not None:
        _ensure_turn(conversation_id, payload.turn_id, at=ts)
        UsageRecordRow.objects.update_or_create(
            turn_id=payload.turn_id,
            defaults={
                "conversation_id": conversation_id,
                "input_tokens": payload.input_tokens,
                "output_tokens": payload.output_tokens,
                "total_tokens": payload.total_tokens,
                "cached_input_tokens": payload.cached_input_tokens,
            },
        )
        return

    if isinstance(payload, CostUpdatedPayload) and payload.turn_id is not None:
        UsageRecordRow.objects.update_or_create(
            turn_id=payload.turn_id,
            defaults={
                "conversation_id": conversation_id,
                "cost": payload.cost,
            },
        )
        return


def sync_active_binding(state: ConversationState) -> None:
    """Mirror the aggregate's active binding into relational binding history.

    The previous active row is closed before the new one is written so the
    one-active-binding constraint never sees two active rows.
    """
    binding = state.binding
    if binding is None:
        return
    cid = state.conversation.id
    ConversationBindingRecord.objects.filter(conversation_id=cid, is_active=True).exclude(
        binding_id=binding.id
    ).update(is_active=False, closed_at=state.conversation.updated_at)
    ConversationBindingRecord.objects.update_or_create(
        binding_id=binding.id,
        defaults={
            "conversation_id": cid,
            "kind": binding.kind.value,
            "configuration": binding.configuration.model_dump(mode="json"),
            "harness_instance_id": binding.harness_instance_id,
            "native_session_id": binding.native_session_id,
            "launch_snapshot": (
                binding.launch_snapshot.model_dump(mode="json")
                if binding.launch_snapshot is not None
                else None
            ),
            "requires_session_recreation": binding.requires_session_recreation,
            "is_active": binding.is_active,
            "created_at": binding.created_at,
            "closed_at": binding.closed_at,
        },
    )


def _recompute_derived_title(state: ConversationState) -> ConversationState:
    """Refresh ``title_derived`` from the earliest retained user message.

    Writes the derived value back into aggregate JSON and copies
    ``display_title`` to the shell column so list, detail, search, and SSE
    observe one title. Native and manual titles are never rewritten.
    """
    cid = state.conversation.id
    earliest = (
        MessageRecord.objects.filter(conversation_id=cid, role=MessageRole.USER.value)
        .order_by("turn__order_index", "order_index", "message_id")
        .first()
    )
    derived = derive_title_from_user_message(earliest.text) if earliest is not None else None
    if derived == state.conversation.title_derived:
        return state
    conversation = state.conversation.model_copy(update={"title_derived": derived})
    updated = state.model_copy(update={"conversation": conversation})
    ConversationAggregate.objects.filter(conversation_id=cid).update(
        title=conversation.display_title,
        state=updated.model_dump(mode="json"),
    )
    return updated


def _refresh_search_document(state: ConversationState) -> None:
    """Rebuild portable search fields from retained projection rows + title."""
    from talktoharnesses.application.search_documents import build_search_document_from_parts

    cid = state.conversation.id
    message_texts = list(
        MessageRecord.objects.filter(conversation_id=cid).values_list("text", flat=True)
    )
    tools = list(ToolRecord.objects.filter(conversation_id=cid))
    fields = build_search_document_from_parts(
        title=state.conversation.display_title,
        message_texts=message_texts,
        tool_names=[t.tool_name for t in tools],
        tool_arguments=[dict(t.arguments) for t in tools],
        tool_paths=[str(p) for t in tools for p in (t.paths or [])],
        tool_output_tails=[t.output_tail for t in tools if t.output_tail],
    )
    SearchDocument.objects.update_or_create(
        conversation_id=cid,
        defaults={
            "owner_id": state.conversation.owner_id,
            "normalized_text": fields.normalized_text,
            "search_title": fields.search_title,
            "search_body": fields.search_body,
            "snippet_text": fields.snippet_text,
            "updated_at": state.conversation.updated_at,
        },
    )


# Silence unused Any if type checkers require the import for dynamic updates.
_ = Any
