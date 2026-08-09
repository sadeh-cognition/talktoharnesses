"""Projection helpers and keyset cursor paging edges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.django.projections import (
    apply_asc_datetime_cursor,
    apply_desc_datetime_cursor,
    apply_desc_int_cursor,
    interaction_from_row,
    page_desc_datetime_uuid,
)
from talktoharnesses.domain import HarnessConfiguration, HarnessKind
from talktoharnesses.domain.enums import ErrorCode, InteractionKind, InteractionStatus
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    ConversationHarnessBinding,
    StructuredQuestionPayload,
)
from talktoharnesses.domain.transitions import new_conversation_state, soft_delete_conversation


def test_projection_cursor_helpers() -> None:
    class _QS:
        def __init__(self) -> None:
            self.filters: list[object] = []

        def filter(self, *args: object, **kwargs: object) -> _QS:
            self.filters.append((args, kwargs))
            return self

    qs = _QS()
    assert apply_desc_datetime_cursor(qs, None, "created_at", "message_id") is qs
    assert apply_asc_datetime_cursor(qs, None, "created_at", "message_id") is qs
    assert apply_desc_int_cursor(qs, None, "order_index", "turn_id") is qs

    from talktoharnesses.application.cursors import encode_cursor

    now = datetime(2026, 8, 9, tzinfo=UTC)
    cursor = encode_cursor(sort=now.isoformat(), id=uuid4())
    apply_desc_datetime_cursor(qs, cursor, "created_at", "message_id")
    apply_asc_datetime_cursor(qs, cursor, "created_at", "message_id")
    int_cursor = encode_cursor(sort="3", id=uuid4())
    apply_desc_int_cursor(qs, int_cursor, "order_index", "turn_id")

    with pytest.raises(DomainError) as bad_dt:
        apply_desc_datetime_cursor(qs, encode_cursor(sort="not-a-date", id=uuid4()), "a", "b")
    assert bad_dt.value.code is ErrorCode.INVALID_CURSOR
    with pytest.raises(DomainError) as bad_int:
        apply_desc_int_cursor(qs, encode_cursor(sort="x", id=uuid4()), "a", "b")
    assert bad_int.value.code is ErrorCode.INVALID_CURSOR

    rows = [SimpleNamespace(created_at=now, message_id=uuid4(), value=i) for i in range(3)]

    def mapper(row: SimpleNamespace) -> int:
        return row.value

    page = page_desc_datetime_uuid(
        rows,
        limit=2,
        sort_attr="created_at",
        id_attr="message_id",
        mapper=mapper,
        cursor=None,
    )
    assert page.items == (0, 1)
    assert page.next_cursor is not None


def test_interaction_from_row_question_branch() -> None:
    row = SimpleNamespace(
        interaction_id=uuid4(),
        kind=InteractionKind.STRUCTURED_QUESTION.value,
        status=InteractionStatus.PENDING.value,
        turn_id=uuid4(),
        request=StructuredQuestionPayload(
            questions=({"id": "q1", "prompt": "q?"},),
        ).model_dump(mode="json"),
        draft=None,
        created_at=datetime.now(UTC),
    )
    projection = interaction_from_row(row)  # type: ignore[arg-type]
    assert projection.kind is InteractionKind.STRUCTURED_QUESTION

    approval_row = SimpleNamespace(
        interaction_id=uuid4(),
        kind=InteractionKind.APPROVAL.value,
        status=InteractionStatus.PENDING.value,
        turn_id=uuid4(),
        request=ApprovalRequestPayload(tool_name="bash").model_dump(mode="json"),
        draft={"x": 1},
        created_at=datetime.now(UTC),
    )
    approval = interaction_from_row(approval_row)  # type: ignore[arg-type]
    assert approval.kind is InteractionKind.APPROVAL
    assert approval.draft == {"x": 1}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_page_messages_tools_and_purge_soft_deleted() -> None:
    from talktoharnesses.django.models import ConversationAggregate

    now = datetime.now(UTC)
    cid = uuid4()
    binding = ConversationHarnessBinding(
        conversation_id=cid,
        kind=HarnessKind.OPENCODE,
        configuration=HarnessConfiguration(kind=HarnessKind.OPENCODE, working_directory="/tmp"),
        created_at=now,
    )
    state = new_conversation_state(
        owner_id="owner",
        now=now,
        binding=binding,
        conversation_id=cid,
    )
    persistence = DjangoPersistence()
    await persistence.save_snapshot(state)

    messages = await persistence.page_messages(cid, "owner", limit=1)
    assert messages.items == ()
    tools = await persistence.page_tools(cid, "owner", limit=1)
    assert tools.items == ()
    turns = await persistence.page_turns(cid, "owner", limit=1)
    assert turns.items == ()

    # Soft-delete via transition then persist deleted_at for purge.
    deleted = soft_delete_conversation(state, now=now - timedelta(days=400))
    await ConversationAggregate.objects.filter(conversation_id=cid).aupdate(
        deleted_at=now - timedelta(days=400),
        state=deleted.state.model_dump(mode="json"),
    )
    await persistence.replace_retention_policy("owner", 1, now=now)
    purged = await persistence.purge_soft_deleted(now)
    assert purged >= 1
