"""Phase 5 API gate: authenticated harness/conversation path without real Grok.

Covers the package surface that must stay green for 2026.8.0.dev5:
issue token → HTTP create harness/conversation → metadata → submit turn →
SSE headers → cross-user isolation → soft-delete.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django import asgi as asgi_mod
from talktoharnesses.django.asgi import reset_service_for_tests
from talktoharnesses.django.auth import issue_token_sync, owner_id_for_user
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 8, 18, 0, 0, tzinfo=UTC)


def _json(
    client: Client,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    **headers: str,
) -> Any:
    data = json.dumps(payload) if payload is not None else None
    request: Any = getattr(client, method)
    return request(
        path,
        data=data,
        content_type="application/json" if payload is not None else None,
        **headers,
    )


@pytest.fixture
def two_users(db: Any) -> tuple[Any, Any]:
    User: Any = get_user_model()
    a = User.objects.create_user(username="owner-a", password="x")
    b = User.objects.create_user(username="owner-b", password="x")
    return a, b


@pytest.fixture
def service(db: Any) -> Any:
    reset_service_for_tests()
    persistence = DjangoPersistence()
    registry = AdapterRegistry()
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    runtime = RuntimeManager(persistence, registry, clock=_now)
    svc = TalkToHarnessesService(persistence, registry, broker, _now, runtime)
    svc._started = True  # type: ignore[attr-defined]
    svc._worker_id = "e2e"  # type: ignore[attr-defined]
    # Phase 5 gate exercises the authenticated API surface without a full
    # worker recovery/probe cycle; mark readiness healthy for /ready.
    coordinator = svc.coordinator
    coordinator._lease_healthy = True  # type: ignore[attr-defined]
    coordinator._heartbeat_healthy = True  # type: ignore[attr-defined]
    coordinator._initial_recovery_complete = True  # type: ignore[attr-defined]
    coordinator._draining = False  # type: ignore[attr-defined]
    coordinator._claims_healthy = True  # type: ignore[attr-defined]
    svc.processor._running = True  # type: ignore[attr-defined]
    svc.processor._claim_task = Mock(done=Mock(return_value=False))  # type: ignore[attr-defined]
    svc._readiness.notify_success(_now())  # type: ignore[attr-defined]
    asgi_mod._service = svc  # type: ignore[attr-defined]

    async def _start_broker() -> None:
        await broker.start()

    asyncio.run(_start_broker())
    yield svc
    asyncio.run(broker.stop())
    reset_service_for_tests()


@pytest.mark.django_db(transaction=True)
def test_phase5_authenticated_conversation_gate(
    service: TalkToHarnessesService, two_users: Any
) -> None:
    user_a, user_b = two_users
    token_a = issue_token_sync(user_a)
    token_b = issue_token_sync(user_b)
    header_a = f"Bearer {token_a.token}"
    header_b = f"Bearer {token_b.token}"
    client = Client()

    # Public surfaces.
    assert client.get("/api/v1/health").status_code == 200
    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200
    assert ready.json() == {"ready": True, "reason": "ready"}

    # Create harness + conversation for A.
    harness = _json(
        client,
        "post",
        "/api/v1/harnesses",
        {
            "name": "local",
            "configuration": {"kind": "grok", "working_directory": "/tmp/ws"},
        },
        HTTP_AUTHORIZATION=header_a,
    )
    assert harness.status_code == 201, harness.content
    harness_id = harness.json()["id"]

    conv = _json(
        client,
        "post",
        "/api/v1/conversations",
        {"harness_id": harness_id, "title": "Phase5 gate"},
        HTTP_AUTHORIZATION=header_a,
    )
    assert conv.status_code == 201, conv.content
    cid = conv.json()["detail"]["conversation"]["id"]

    # Metadata mutations.
    pinned = client.post(
        f"/api/v1/conversations/{cid}/pin",
        HTTP_AUTHORIZATION=header_a,
    )
    assert pinned.status_code == 200
    assert pinned.json()["detail"]["conversation"]["pinned_at"] is not None

    # Submit turn (durable command acceptance; no real adapter execution required).
    turn = client.post(
        f"/api/v1/conversations/{cid}/turns",
        data=json.dumps({"prompt": "hello phase5"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=header_a,
        HTTP_IDEMPOTENCY_KEY="e2e-k1",
    )
    assert turn.status_code == 202, turn.content
    body = turn.json()
    assert body["command"]["idempotency_key"] == "e2e-k1"
    assert body["turn"]["status"] == "queued"

    # Idempotent replay.
    again = client.post(
        f"/api/v1/conversations/{cid}/turns",
        data=json.dumps({"prompt": "hello phase5"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=header_a,
        HTTP_IDEMPOTENCY_KEY="e2e-k1",
    )
    assert again.status_code == 202
    assert again.json()["command"]["id"] == body["command"]["id"]

    # SSE surface.
    events = client.get(
        f"/api/v1/conversations/{cid}/events",
        HTTP_AUTHORIZATION=header_a,
    )
    assert events.status_code == 200
    assert events["Content-Type"].startswith("text/event-stream")
    assert events["Cache-Control"] == "no-cache"
    assert events["X-Accel-Buffering"] == "no"

    # Cross-user isolation: B cannot see A's conversation or harness.
    missing = client.get(
        f"/api/v1/conversations/{cid}",
        HTTP_AUTHORIZATION=header_b,
    )
    assert missing.status_code == 404
    foreign_harness = client.get(
        f"/api/v1/harnesses/{harness_id}/capabilities",
        HTTP_AUTHORIZATION=header_b,
    )
    assert foreign_harness.status_code == 404
    b_list = client.get("/api/v1/conversations", HTTP_AUTHORIZATION=header_b)
    assert b_list.status_code == 200
    assert b_list.json()["items"] == []

    # Soft-delete requires no active/queued work.
    cancel = client.delete(
        f"/api/v1/conversations/{cid}/queued-prompt",
        HTTP_AUTHORIZATION=header_a,
    )
    assert cancel.status_code in {200, 204}, cancel.content
    deleted = client.delete(
        f"/api/v1/conversations/{cid}",
        HTTP_AUTHORIZATION=header_a,
    )
    assert deleted.status_code == 204, deleted.content
    gone = client.get(
        f"/api/v1/conversations/{cid}",
        HTTP_AUTHORIZATION=header_a,
    )
    assert gone.status_code == 404

    # Owner id derivation is the authenticated user primary key string.
    assert owner_id_for_user(user_a) == str(user_a.pk)


@pytest.mark.django_db(transaction=True)
def test_core_facade_snapshot_sequence_advances(
    service: TalkToHarnessesService, two_users: Any
) -> None:
    """Facade + persistence sequence stamps stay consistent after mutations."""
    user_a, _ = two_users
    owner = owner_id_for_user(user_a)

    async def run() -> None:
        from talktoharnesses.domain import HarnessConfiguration, HarnessKind

        h = await service.create_harness(
            owner,
            name="h",
            configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        )
        snap0 = await service.create_conversation(owner, h.id, title="seq")
        assert snap0.sequence == 0
        cid = snap0.detail.conversation.id
        pinned = await service.pin_conversation(owner, cid)
        assert pinned.sequence >= 1
        hw = await service.get_high_water_sequence(owner, cid)
        assert hw == pinned.sequence

    asyncio.run(run())
