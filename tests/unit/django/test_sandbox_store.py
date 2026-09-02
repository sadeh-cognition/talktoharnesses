"""DjangoSandboxStore round-trip tests (SQLite path in CI)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tth_types.enums import HarnessKind

from talktoharnesses.django.sandbox_store import DjangoSandboxStore
from talktoharnesses.remote.sandbox import SandboxRecordData


def _record(*, token: str = "token-1", status: str = "preparing") -> SandboxRecordData:
    now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    return SandboxRecordData(
        kind=HarnessKind.GROK,
        container_name="tth-grok",
        image="tth-grok:latest",
        host_port=8111,
        base_url="http://127.0.0.1:8111",
        split_token=token,
        status=status,
        created_at=now,
        updated_at=now,
        last_ready_at=None,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_kind() -> None:
    store = DjangoSandboxStore()
    assert await store.get(HarnessKind.GROK) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_upsert_then_get_round_trips() -> None:
    store = DjangoSandboxStore()
    record = _record()
    await store.upsert(record)
    loaded = await store.get(HarnessKind.GROK)
    assert loaded == record


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_upsert_updates_existing_row() -> None:
    store = DjangoSandboxStore()
    await store.upsert(_record())
    ready_at = datetime(2026, 8, 30, 12, 5, 0, tzinfo=UTC)
    updated = _record(status="ready").model_copy(update={"last_ready_at": ready_at})
    await store.upsert(updated)
    loaded = await store.get(HarnessKind.GROK)
    assert loaded is not None
    assert loaded.status == "ready"
    assert loaded.last_ready_at == ready_at
    assert loaded.split_token == "token-1"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_reserve_inserts_when_kind_is_new() -> None:
    store = DjangoSandboxStore()
    record = _record()
    stored = await store.reserve(record)
    assert stored == record
    assert await store.get(HarnessKind.GROK) == record


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_reserve_returns_existing_row_and_keeps_its_token() -> None:
    """Two workers preparing the same kind must both end up on one token."""
    store = DjangoSandboxStore()
    first = await store.reserve(_record(token="token-first"))
    second = await store.reserve(_record(token="token-second"))
    assert first.split_token == "token-first"
    assert second.split_token == "token-first"
    loaded = await store.get(HarnessKind.GROK)
    assert loaded is not None
    assert loaded.split_token == "token-first"
