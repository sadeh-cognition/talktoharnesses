"""Map ORM projection rows to shared Pydantic wire models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel

from talktoharnesses.application.cursors import decode_cursor, encode_cursor
from talktoharnesses.django.models import (
    ActivityRecord,
    ConversationAggregate,
    HarnessRecord,
    InteractionRecord,
    MessageRecord,
    PlanRecord,
    ToolRecord,
    TurnRecord,
)
from talktoharnesses.domain.enums import (
    ActivityStatus,
    CommandKind,
    CommandStatus,
    ConversationStatus,
    ErrorCode,
    HarnessKind,
    InteractionKind,
    InteractionStatus,
    MessageRole,
    ToolOutcome,
    TurnStatus,
)
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    ActivityProjection,
    ApprovalRequestPayload,
    Command,
    CommandProjection,
    ConversationShell,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessProbeProjection,
    HarnessProjection,
    InteractionProjection,
    MessageProjection,
    Page,
    PlanItem,
    PlanProjection,
    StructuredQuestionPayload,
    ToolProjection,
    TurnProjection,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def not_found(resource: str = "conversation") -> DomainError:
    return DomainError(ErrorCode.NOT_FOUND, f"{resource} not found")


def shell_from_row(row: ConversationAggregate) -> ConversationShell:
    return ConversationShell(
        id=row.conversation_id,
        title=row.title or "Untitled conversation",
        status=ConversationStatus(row.status),
        harness_kind=HarnessKind(row.harness_kind) if row.harness_kind else None,
        model=row.model,
        mode=row.mode,
        has_pending_interactions=row.has_pending_interactions,
        pinned_at=row.pinned_at,
        archived_at=row.archived_at,
        snoozed_until=row.snoozed_until,
        updated_at=row.updated_at,
        latest_activity_at=row.latest_activity_at,
    )


def _validate_json(model: type[_ModelT], value: object) -> _ModelT:
    import json

    return model.model_validate_json(json.dumps(value))


def _uuid_attr(row: object, name: str) -> UUID:
    return cast(UUID, getattr(row, name))


def harness_from_row(row: HarnessRecord) -> HarnessProjection:
    return HarnessProjection(
        id=row.harness_id,
        owner_id=row.owner_id,
        name=row.name,
        kind=HarnessKind(row.kind),
        configuration=_validate_json(HarnessConfiguration, row.configuration),
        created_at=row.created_at,
    )


def probe_from_row(row: HarnessRecord) -> HarnessProbeProjection:
    if row.last_probe is None or row.last_probed_at is None:
        raise not_found("harness probe")
    return HarnessProbeProjection(
        harness_id=row.harness_id,
        capabilities=_validate_json(HarnessCapabilities, row.last_probe),
        probed_at=row.last_probed_at,
    )


def turn_from_row(row: TurnRecord) -> TurnProjection:
    return TurnProjection(
        id=row.turn_id,
        conversation_id=_uuid_attr(row, "conversation_id"),
        status=TurnStatus(row.status),
        user_message_id=row.user_message_id,
        command_id=row.command_id,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        terminal_reason=row.terminal_reason,
    )


def message_from_row(row: MessageRecord) -> MessageProjection:
    return MessageProjection(
        id=row.message_id,
        turn_id=_uuid_attr(row, "turn_id"),
        role=MessageRole(row.role),
        text=row.text,
        sequence=row.sequence,
        interrupted=row.interrupted,
        created_at=row.created_at,
    )


def tool_from_row(row: ToolRecord) -> ToolProjection:
    paths = tuple(str(p) for p in (row.paths or []))
    args = cast(dict[str, Any], row.arguments)
    return ToolProjection(
        id=row.tool_id,
        turn_id=_uuid_attr(row, "turn_id"),
        tool_name=row.tool_name,
        arguments=dict(args),
        outcome=ToolOutcome(row.outcome),
        exit_status=row.exit_status,
        paths=paths,
        output_tail=row.output_tail,
    )


def plan_from_row(row: PlanRecord) -> PlanProjection:
    items = tuple(_validate_json(PlanItem, item) for item in (row.items or []))
    return PlanProjection(
        id=row.plan_id,
        turn_id=_uuid_attr(row, "turn_id"),
        items=items,
    )


def activity_from_row(row: ActivityRecord) -> ActivityProjection:
    return ActivityProjection(
        id=row.activity_id,
        conversation_id=_uuid_attr(row, "conversation_id"),
        parent_turn_id=row.parent_turn_id,
        parent_activity_id=row.parent_activity_id,
        status=ActivityStatus(row.status),
        title=row.title,
        summary=row.summary,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def interaction_from_row(row: InteractionRecord) -> InteractionProjection:
    request_data: dict[str, object] = row.request
    kind = InteractionKind(row.kind)
    if kind is InteractionKind.APPROVAL:
        request: ApprovalRequestPayload | StructuredQuestionPayload = _validate_json(
            ApprovalRequestPayload, request_data
        )
    else:
        request = _validate_json(StructuredQuestionPayload, request_data)
    draft = cast(dict[str, Any] | None, row.draft)
    return InteractionProjection(
        id=row.interaction_id,
        kind=kind,
        status=InteractionStatus(row.status),
        turn_id=row.turn_id,
        request=request,
        draft=draft,
        created_at=row.created_at,
    )


def command_projection(command: Command) -> CommandProjection:
    return CommandProjection(
        id=command.id,
        kind=command.kind,
        status=command.status,
        target_turn_id=command.target_turn_id,
        idempotency_key=command.idempotency_key,
        created_at=command.created_at,
    )


def _dt_sort(value: datetime) -> str:
    return value.isoformat()


def page_desc_datetime_uuid(
    rows: list[Any],
    *,
    limit: int,
    sort_attr: str,
    id_attr: str,
    mapper: Any,
    cursor: str | None,
) -> Page[Any]:
    """Keyset page with (sort DESC, id DESC)."""
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        sort_val = getattr(last, sort_attr)
        item_id = getattr(last, id_attr)
        sort_key = _dt_sort(sort_val) if isinstance(sort_val, datetime) else str(sort_val)
        next_cursor = encode_cursor(sort=sort_key, id=item_id)
    return Page(items=tuple(mapper(r) for r in items), next_cursor=next_cursor)


def apply_desc_datetime_cursor(qs: Any, cursor: str | None, sort_field: str, id_field: str) -> Any:
    if cursor is None:
        return qs
    sort, item_id = decode_cursor(cursor)
    # (sort, id) < (cursor_sort, cursor_id) in DESC order means
    # sort < cursor_sort OR (sort == cursor_sort AND id < cursor_id)
    from django.db.models import Q

    try:
        sort_dt = datetime.fromisoformat(sort)
    except ValueError as exc:
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor") from exc
    return qs.filter(
        Q(**{f"{sort_field}__lt": sort_dt})
        | (Q(**{sort_field: sort_dt}) & Q(**{f"{id_field}__lt": item_id}))
    )


def apply_asc_datetime_cursor(qs: Any, cursor: str | None, sort_field: str, id_field: str) -> Any:
    if cursor is None:
        return qs
    sort, item_id = decode_cursor(cursor)
    from django.db.models import Q

    try:
        sort_dt = datetime.fromisoformat(sort)
    except ValueError as exc:
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor") from exc
    return qs.filter(
        Q(**{f"{sort_field}__gt": sort_dt})
        | (Q(**{sort_field: sort_dt}) & Q(**{f"{id_field}__gt": item_id}))
    )


def apply_desc_int_cursor(qs: Any, cursor: str | None, sort_field: str, id_field: str) -> Any:
    if cursor is None:
        return qs
    sort, item_id = decode_cursor(cursor)
    from django.db.models import Q

    try:
        sort_int = int(sort)
    except ValueError as exc:
        raise DomainError(ErrorCode.INVALID_CURSOR, "invalid cursor") from exc
    return qs.filter(
        Q(**{f"{sort_field}__lt": sort_int})
        | (Q(**{sort_field: sort_int}) & Q(**{f"{id_field}__lt": item_id}))
    )


# Silence unused imports for re-export convenience in type-heavy module.
_ = (CommandKind, CommandStatus, UUID)
