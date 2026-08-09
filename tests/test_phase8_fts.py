"""Phase 8 full-text search parity (SQLite FTS5; PostgreSQL in CI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
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
    assert [s.id for s in by_title.items] == [state.conversation.id]

    by_user = await persistence.search_conversations("owner", "WIDGET")
    assert [s.id for s in by_user.items] == [state.conversation.id]

    by_assistant = await persistence.search_conversations("owner", "gasket")
    assert [s.id for s in by_assistant.items] == [state.conversation.id]

    by_tool = await persistence.search_conversations("owner", "bash listing")
    assert [s.id for s in by_tool.items] == [state.conversation.id]

    both = await persistence.search_conversations("owner", "widget missingterm")
    assert both.items == ()

    punct = await persistence.search_conversations("owner", "widget-assembly!")
    assert [s.id for s in punct.items] == [state.conversation.id]


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
    assert {s.id for s in a_hits.items} == {a.conversation.id}

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
    assert [s.id for s in page.items] == [second.conversation.id]
    assert page.next_cursor is not None
    page2 = await persistence.search_conversations(
        "owner", "order-token", cursor=page.next_cursor, limit=1
    )
    assert [s.id for s in page2.items] == [first.conversation.id]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_empty_query_and_content_update_sync() -> None:
    persistence = DjangoPersistence()
    state = idle_state(title="trigger-sync-token")
    await persistence.save_snapshot(state)
    empty = await persistence.search_conversations("owner", "   !!! ")
    assert empty.items == ()

    hits = await persistence.search_conversations("owner", "trigger-sync-token")
    assert [s.id for s in hits.items] == [state.conversation.id]

    doc = await SearchDocument.objects.aget(conversation_id=state.conversation.id)
    doc.normalized_text = "brand new indexed phrase"
    doc.updated_at = datetime.now(UTC)
    await doc.asave()
    updated = await persistence.search_conversations("owner", "brand new")
    assert [s.id for s in updated.items] == [state.conversation.id]
    stale = await persistence.search_conversations("owner", "trigger-sync-token")
    assert stale.items == ()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fts_preserves_diacritics_as_literal_terms() -> None:
    persistence = DjangoPersistence()
    state = idle_state(title="café notes")
    await persistence.save_snapshot(state)

    accented = await persistence.search_conversations("owner", "café")
    assert [shell.id for shell in accented.items] == [state.conversation.id]
    unaccented = await persistence.search_conversations("owner", "cafe")
    assert unaccented.items == ()


@pytest.mark.django_db(transaction=True)
def test_fts_migration_reverse_preserves_search_document() -> None:
    from django.core.management import call_command
    from django.db import connection as db

    persistence_setup = DjangoPersistence()
    # Ensure a real aggregate exists so the FK content row is valid.
    from asgiref.sync import async_to_sync

    state = idle_state(title="retain me please")
    async_to_sync(persistence_setup.save_snapshot)(state)
    assert SearchDocument.objects.filter(conversation_id=state.conversation.id).exists()

    call_command("migrate", "talktoharnesses", "0005_phase8_backfill", verbosity=0)
    assert SearchDocument.objects.filter(conversation_id=state.conversation.id).exists()
    if db.vendor == "sqlite":
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='talktoharnesses_search_document_fts'"
            )
            assert cursor.fetchone() is None
    elif db.vendor == "postgresql":
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name='search_vector'",
                ["talktoharnesses_search_document"],
            )
            assert cursor.fetchone() is None
    call_command("migrate", "talktoharnesses", "0006_phase8_fts", verbosity=0)
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='talktoharnesses_search_document_fts'"
            )
            assert cursor.fetchone() is not None
