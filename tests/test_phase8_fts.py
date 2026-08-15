"""Phase 8 full-text search parity (SQLite FTS5; PostgreSQL in CI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.phase8_fixtures import NOW, commit_turn, idle_state

from talktoharnesses.django.models import SearchDocument
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import soft_delete_conversation


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_matches_title_messages_tools_and_and_terms() -> None:
    persistence = DjangoPersistence()
    state = idle_state(title="Alpha Project Notes")
    await persistence.save_snapshot(state)
    await commit_turn(
        persistence,
        state,
        prompt="user wrote about widget assembly",
        key="t1",
        now=NOW,
        assistant_text="assistant mentions gasket torque",
        tool=True,
    )

    by_title = await persistence.search_conversations("owner", "Alpha Project")
    assert [h.conversation.id for h in by_title.items] == [state.conversation.id]

    by_user = await persistence.search_conversations("owner", "WIDGET")
    assert [h.conversation.id for h in by_user.items] == [state.conversation.id]

    by_assistant = await persistence.search_conversations("owner", "gasket")
    assert [h.conversation.id for h in by_assistant.items] == [state.conversation.id]

    by_tool = await persistence.search_conversations("owner", "bash listing")
    assert [h.conversation.id for h in by_tool.items] == [state.conversation.id]

    both = await persistence.search_conversations("owner", "widget missingterm")
    assert both.items == ()

    punct = await persistence.search_conversations("owner", "widget-assembly!")
    assert [h.conversation.id for h in punct.items] == [state.conversation.id]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_owner_isolation_soft_delete_and_removed_content() -> None:
    persistence = DjangoPersistence()
    a = idle_state("owner-a", title="shared-token-alpha")
    b = idle_state("owner-b", title="shared-token-beta")
    await persistence.save_snapshot(a)
    await persistence.save_snapshot(b)
    a = await commit_turn(persistence, a, prompt="keep me", key="a1", now=NOW)
    await commit_turn(persistence, b, prompt="other owner", key="b1", now=NOW)

    a_hits = await persistence.search_conversations("owner-a", "shared-token")
    assert {h.conversation.id for h in a_hits.items} == {a.conversation.id}

    deleted = soft_delete_conversation(a, now=NOW + timedelta(minutes=1))
    await persistence.commit_facade_mutation(
        a.conversation.id,
        "owner-a",
        a.conversation.version,
        deleted.state,
        deleted.events,
    )
    after_delete = await persistence.search_conversations("owner-a", "shared-token")
    assert after_delete.items == ()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_keyset_order_matches_list_order() -> None:
    persistence = DjangoPersistence()
    first = idle_state(title="order-token")
    second = idle_state(title="order-token")
    await persistence.save_snapshot(first)
    later = NOW + timedelta(minutes=5)
    second = second.model_copy(
        update={"conversation": second.conversation.model_copy(update={"updated_at": later})}
    )
    await persistence.save_snapshot(second)

    page = await persistence.search_conversations("owner", "order-token", limit=1)
    assert [h.conversation.id for h in page.items] == [second.conversation.id]
    assert page.next_cursor is not None
    page2 = await persistence.search_conversations(
        "owner", "order-token", cursor=page.next_cursor, limit=1
    )
    assert [h.conversation.id for h in page2.items] == [first.conversation.id]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_uses_fixed_rank_and_database_exclusions() -> None:
    persistence = DjangoPersistence()
    title_match = idle_state(title="alpha")
    body_match = idle_state(title="other")
    body_match = body_match.model_copy(
        update={
            "conversation": body_match.conversation.model_copy(
                update={"updated_at": NOW + timedelta(days=1)}
            )
        }
    )
    await persistence.save_snapshot(title_match)
    await persistence.save_snapshot(body_match)
    body_document = await SearchDocument.objects.aget(conversation_id=body_match.conversation.id)
    body_document.normalized_text = "alpha alpha alpha alpha alpha alpha alpha alpha blocked"
    body_document.search_title = ""
    body_document.search_body = "alpha alpha alpha alpha alpha alpha alpha alpha blocked"
    body_document.snippet_text = "alpha alpha alpha alpha alpha alpha alpha alpha blocked"
    await body_document.asave()

    ranked = await persistence.search_conversations("owner", "alpha")
    assert [hit.conversation.id for hit in ranked.items] == [
        title_match.conversation.id,
        body_match.conversation.id,
    ]
    excluded = await persistence.search_conversations("owner", "alpha -blocked")
    assert [hit.conversation.id for hit in excluded.items] == [title_match.conversation.id]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_empty_query_and_content_update_sync() -> None:
    persistence = DjangoPersistence()
    state = idle_state(title="trigger-sync-token")
    await persistence.save_snapshot(state)
    from talktoharnesses.domain.enums import ErrorCode
    from talktoharnesses.domain.errors import DomainError

    with pytest.raises(DomainError) as empty_exc:
        await persistence.search_conversations("owner", "   !!! ")
    assert empty_exc.value.code is ErrorCode.INVALID_SEARCH_QUERY

    hits = await persistence.search_conversations("owner", "trigger-sync-token")
    assert [h.conversation.id for h in hits.items] == [state.conversation.id]

    doc = await SearchDocument.objects.aget(conversation_id=state.conversation.id)
    doc.normalized_text = "brand new indexed phrase"
    doc.search_title = ""
    doc.search_body = "brand new indexed phrase"
    doc.snippet_text = "brand new indexed phrase"
    doc.updated_at = datetime.now(UTC)
    await doc.asave()
    updated = await persistence.search_conversations("owner", "brand new")
    assert [h.conversation.id for h in updated.items] == [state.conversation.id]
    stale = await persistence.search_conversations("owner", "trigger-sync-token")
    assert stale.items == ()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_preserves_diacritics_as_literal_terms() -> None:
    persistence = DjangoPersistence()
    state = idle_state(title="café notes")
    await persistence.save_snapshot(state)

    accented = await persistence.search_conversations("owner", "café")
    assert [hit.conversation.id for hit in accented.items] == [state.conversation.id]
    unaccented = await persistence.search_conversations("owner", "cafe")
    assert unaccented.items == ()
