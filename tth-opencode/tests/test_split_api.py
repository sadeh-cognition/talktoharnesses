"""HTTP-level contract tests for the split API over a fake adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from django.test import AsyncClient
from tth_types.adapter import TurnRequest
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessConfiguration
from tth_types.split_api import (
    FRAME_END,
    FRAME_HARNESS_EVENT,
    CreateSessionRequest,
    HarnessEventFrame,
    ProbeRequest,
    ProbeResponse,
    SessionCreated,
    SplitError,
)

from tests.conftest import FakeAdapter


def _config(tmp_path_str: str) -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=HarnessKind.OPENCODE,
        working_directory=tmp_path_str,
    )


async def _post(client: AsyncClient, path: str, body: Any) -> Any:
    return await client.post(path, data=body.model_dump_json(), content_type="application/json")


async def _create_session(
    client: AsyncClient, tmp_path_str: str
) -> SessionCreated:
    request = CreateSessionRequest(
        mode="start",
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=_config(tmp_path_str),
        adapter_version="test",
        redaction_patterns=("secret",),
        seen_native_ids=("imported-1",),
    )
    response = await _post(client, "/v1/sessions", request)
    assert response.status_code == 201, response.content
    return SessionCreated.model_validate_json(response.content)


async def _read_frames(response: Any, limit: int) -> list[tuple[str, str]]:
    frames: list[tuple[str, str]] = []
    buffer = b""
    content: AsyncIterator[bytes] = response.streaming_content
    async for chunk in content:
        buffer += chunk
        while b"\n\n" in buffer:
            raw, buffer = buffer.split(b"\n\n", 1)
            text = raw.decode()
            if text.startswith(":"):
                continue
            event = ""
            data = ""
            for line in text.splitlines():
                if line.startswith("event: "):
                    event = line[len("event: ") :]
                elif line.startswith("data: "):
                    data = line[len("data: ") :]
            frames.append((event, data))
            if len(frames) >= limit or event == FRAME_END:
                return frames
    return frames


async def test_health(fake_adapter: FakeAdapter) -> None:
    client = AsyncClient()
    response = await client.get("/v1/health")
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["kind"] == "opencode"
    assert body["sessions"] == 0
    assert body["tth_types_version"]


async def test_probe(fake_adapter: FakeAdapter, tmp_path: Any) -> None:
    client = AsyncClient()
    request = ProbeRequest(
        configuration=_config(str(tmp_path)),
        adapter_version="test",
        redaction_patterns=("secret",),
    )
    response = await _post(client, "/v1/probe", request)
    assert response.status_code == 200, response.content
    probe = ProbeResponse.model_validate_json(response.content)
    assert probe.capabilities.kind is HarnessKind.OPENCODE
    assert probe.launch.working_directory == str(tmp_path.resolve())
    assert probe.launch.resolved_executable is None
    assert probe.advisory is not None
    assert fake_adapter.redaction_patterns == ("secret",)


async def test_session_lifecycle_and_event_stream(
    fake_adapter: FakeAdapter, tmp_path: Any
) -> None:
    client = AsyncClient()
    created = await _create_session(client, str(tmp_path))
    assert created.pid is None
    assert created.session.metadata["split_session_id"] == str(created.session_id)
    assert fake_adapter.preflight_modes == ["start"]
    assert fake_adapter._imported is not None  # pyright: ignore[reportPrivateUsage]

    turn = TurnRequest(turn_id=uuid4(), prompt="hello")
    response = await _post(client, f"/v1/sessions/{created.session_id}/turns", turn)
    assert response.status_code == 204, response.content

    events_response = await client.get(f"/v1/sessions/{created.session_id}/events")
    assert events_response.status_code == 200
    assert events_response["Content-Type"] == "text/event-stream"
    frames = await _read_frames(events_response, limit=3)
    assert [name for name, _ in frames] == [FRAME_HARNESS_EVENT] * 3
    decoded = [HarnessEventFrame.model_validate_json(data) for _, data in frames]
    assert decoded[0].item.type == "assistant_message_started"
    assert decoded[1].item.type == "assistant_message_completed"
    assert decoded[2].item.type == "turn_completed"
    # The dedupe delta surfaces exactly once, on the first frame after marking.
    all_new_ids = [nid for frame in decoded for nid in frame.new_native_ids]
    assert all_new_ids == [f"native-{turn.turn_id}"]

    delete_response = await client.delete(f"/v1/sessions/{created.session_id}")
    assert delete_response.status_code == 204
    assert fake_adapter.closed

    health = json.loads((await client.get("/v1/health")).content)
    assert health["sessions"] == 0


async def test_steer_interrupt_answers(fake_adapter: FakeAdapter, tmp_path: Any) -> None:
    client = AsyncClient()
    created = await _create_session(client, str(tmp_path))
    sid = created.session_id

    from tth_types.adapter import SteerRequest

    steer = await _post(
        client, f"/v1/sessions/{sid}/steer", SteerRequest(turn_id=uuid4(), prompt="go")
    )
    assert steer.status_code == 200
    assert json.loads(steer.content)["accepted"] is True

    interrupt = await client.post(f"/v1/sessions/{sid}/interrupt")
    assert interrupt.status_code == 204
    assert fake_adapter.interrupted

    from tth_types.harness import InteractionAnswer

    answer = InteractionAnswer(interaction_id=uuid4())
    answered = await _post(client, f"/v1/sessions/{sid}/answers", answer)
    assert answered.status_code == 204
    assert fake_adapter.answers[0].interaction_id == answer.interaction_id


async def test_unknown_session_is_404(fake_adapter: FakeAdapter) -> None:
    client = AsyncClient()
    response = await client.post(f"/v1/sessions/{uuid4()}/interrupt")
    assert response.status_code == 404
    error = SplitError.model_validate_json(response.content)
    assert error.code == "not_found"


async def test_invalid_body_is_422(fake_adapter: FakeAdapter) -> None:
    client = AsyncClient()
    response = await client.post(
        "/v1/probe", data='{"nope": 1}', content_type="application/json"
    )
    assert response.status_code == 422
    assert SplitError.model_validate_json(response.content).code == "validation_error"


async def test_terminate_closes_session(fake_adapter: FakeAdapter, tmp_path: Any) -> None:
    client = AsyncClient()
    created = await _create_session(client, str(tmp_path))
    response = await client.post(
        f"/v1/sessions/{created.session_id}/terminate",
        data='{"reason": "test"}',
        content_type="application/json",
    )
    assert response.status_code == 204
    assert fake_adapter.closed


async def test_split_token_auth(
    fake_adapter: FakeAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTH_SPLIT_TOKEN", "s3cret-token")
    client = AsyncClient()
    # Health stays open for container healthchecks.
    assert (await client.get("/v1/health")).status_code == 200
    denied = await client.post(f"/v1/sessions/{uuid4()}/interrupt")
    assert denied.status_code == 401
    allowed = await client.post(
        f"/v1/sessions/{uuid4()}/interrupt",
        headers={"X-TTH-Split-Token": "s3cret-token"},
    )
    assert allowed.status_code == 404


async def test_capacity_refusal_closes_started_session(
    fake_adapter: FakeAdapter, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """store.add raising CONVERSATION_BUSY must not orphan the started session."""
    from tth_types.enums import ErrorCode

    from tth_opencode.sessions import get_session_store

    store = get_session_store()
    monkeypatch.setattr(
        store,
        "_policy",
        store._policy.model_copy(  # pyright: ignore[reportPrivateUsage]
            update={"max_runtimes": 0}
        ),
    )
    client = AsyncClient()
    request = CreateSessionRequest(
        mode="start",
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=_config(str(tmp_path)),
        adapter_version="test",
    )

    response = await _post(client, "/v1/sessions", request)

    assert response.status_code == 409, response.content
    error = SplitError.model_validate_json(response.content)
    assert error.code == ErrorCode.CONVERSATION_BUSY.value
    assert fake_adapter.closed
