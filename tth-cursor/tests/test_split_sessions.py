"""Split session lifecycle over HTTP: detach, reattach and replay.

Vendored identically into every split (see scripts/check_split_drift.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from django.test import AsyncClient
from tth_types.harness import HarnessConfiguration
from tth_types.split_api import (
    FRAME_END,
    FRAME_HARNESS_EVENT,
    CreateSessionRequest,
    SessionCreated,
    SplitError,
)

from tth_cursor.service import KIND
from tth_cursor.sessions import get_session_store

if TYPE_CHECKING:
    from tests.conftest import FakeAdapter


def _request(working_directory: str) -> CreateSessionRequest:
    return CreateSessionRequest(
        mode="start",
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=HarnessConfiguration(kind=KIND, working_directory=working_directory),
        adapter_version="test",
    )


async def _create(client: AsyncClient, request: CreateSessionRequest) -> SessionCreated:
    response = await client.post(
        "/v1/sessions", data=request.model_dump_json(), content_type="application/json"
    )
    assert response.status_code == 201, response.content
    return SessionCreated.model_validate_json(response.content)


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_streaming_an_unknown_session_is_not_found(fake_adapter: FakeAdapter) -> None:
    response = await AsyncClient().get(f"/v1/sessions/{uuid4()}/events?after=0")

    assert response.status_code == 404


async def test_a_dropped_stream_detaches_the_session_until_the_grace_period_ends(
    fake_adapter: FakeAdapter, tmp_path: Any
) -> None:
    client = AsyncClient()
    sid = (await _create(client, _request(str(tmp_path)))).session_id
    store = get_session_store()
    store._policy = store._policy.model_copy(update={"detach_grace": 0.05})  # pyright: ignore[reportPrivateUsage]
    await store.get(sid).enqueue(FRAME_HARNESS_EVENT, "{}")
    response: Any = await client.get(f"/v1/sessions/{sid}/events")
    content: AsyncIterator[bytes] = response.streaming_content

    await anext(content)
    response.close()
    await _settle()

    # The harness keeps running so a restarted proxy can reattach.
    entry = store.get(sid)
    assert (entry.expiry is not None, entry.last_sent_id) == (True, 1)
    assert not fake_adapter.closed

    await asyncio.sleep(0.1)
    assert len(store) == 0
    assert fake_adapter.closed


async def test_a_stream_that_delivered_the_end_frame_closes_the_session(
    fake_adapter: FakeAdapter, tmp_path: Any
) -> None:
    client = AsyncClient()
    sid = (await _create(client, _request(str(tmp_path)))).session_id
    store = get_session_store()
    await store.get(sid).enqueue(FRAME_END, "{}")
    response: Any = await client.get(f"/v1/sessions/{sid}/events")
    content: AsyncIterator[bytes] = response.streaming_content

    await anext(content)
    response.close()
    await _settle()

    # Nothing is left to reattach for, so the session closes without a grace period.
    assert len(store) == 0
    assert fake_adapter.closed


async def test_reattach_replays_the_frames_after_the_cursor(
    fake_adapter: FakeAdapter, tmp_path: Any
) -> None:
    client = AsyncClient()
    sid = (await _create(client, _request(str(tmp_path)))).session_id
    store = get_session_store()
    for _ in range(3):
        await store.get(sid).enqueue(FRAME_HARNESS_EVENT, "{}")
    first: Any = await client.get(f"/v1/sessions/{sid}/events")
    first_content: AsyncIterator[bytes] = first.streaming_content
    for _ in range(3):
        await anext(first_content)
    first.close()
    await _settle()

    # The proxy committed frame 1 before it died; frames 2 and 3 come again.
    again: Any = await client.get(f"/v1/sessions/{sid}/events?after=1")
    again_content: AsyncIterator[bytes] = again.streaming_content
    replayed = [await anext(again_content), await anext(again_content)]

    assert [frame.split(b"\n")[1] for frame in replayed] == [b"id: 2", b"id: 3"]
    assert store.get(sid).expiry is None
    again.close()
    await store.close(sid)


@pytest.mark.parametrize(("sent", "after"), [(0, 5), (3, 0), (0, -1)])
async def test_reattach_after_a_cursor_the_split_cannot_serve_is_refused(
    fake_adapter: FakeAdapter, tmp_path: Any, sent: int, after: int
) -> None:
    client = AsyncClient()
    sid = (await _create(client, _request(str(tmp_path)))).session_id
    store = get_session_store()
    # Frames sent before the cursor but no longer retained cannot be replayed.
    store.get(sid).last_sent_id = sent

    response = await client.get(f"/v1/sessions/{sid}/events?after={after}")

    assert response.status_code == 409
    assert SplitError.model_validate_json(response.content).code == "invalid_cursor"
    await store.close(sid)


async def test_a_new_session_for_the_same_binding_replaces_the_orphan(
    fake_adapter: FakeAdapter, tmp_path: Any
) -> None:
    client = AsyncClient()
    request = _request(str(tmp_path))
    first = await _create(client, request)
    second_request = request.model_copy(update={"session_id": uuid4()})

    second = await _create(client, second_request)

    assert second.session_id == second_request.session_id
    assert len(get_session_store()) == 1
    assert (await client.delete(f"/v1/sessions/{first.session_id}")).status_code == 404
    await get_session_store().close(second.session_id)
