"""Phase 8 gate: durable harness switching over the packaged HTTP surface.

Uses Django persistence and SDK-managed fake adapters so a switch exercises
candidate creation, the atomic binding replacement, and promotion without a
real provider process.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from tests.runtime.conftest import FakeAdapter

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django import asgi as asgi_mod
from talktoharnesses.django.asgi import reset_service_for_tests
from talktoharnesses.django.auth import issue_token_sync, owner_id_for_user
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import CommandKind, HarnessKind
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 9, 10, 0, 0, tzinfo=UTC)


class _SwitchAdapter(FakeAdapter):
    sdk_managed = True

    def __init__(self, kind: HarnessKind) -> None:
        super().__init__()
        self.kind = kind


def _json(
    client: Client,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    **headers: str,
) -> Any:
    request: Any = getattr(client, method)
    return request(
        path,
        data=json.dumps(payload) if payload is not None else None,
        content_type="application/json" if payload is not None else None,
        **headers,
    )


@pytest.fixture
def user(db: Any) -> Any:
    User: Any = get_user_model()
    return User.objects.create_user(username="p8-switch", password="x")


@pytest.fixture
def service(db: Any) -> Any:
    reset_service_for_tests()
    persistence = DjangoPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _SwitchAdapter(HarnessKind.GROK))  # type: ignore[arg-type]
    registry.register(HarnessKind.CODEX, lambda: _SwitchAdapter(HarnessKind.CODEX))  # type: ignore[arg-type]
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    runtime = RuntimeManager(persistence, registry, clock=_now)
    svc = TalkToHarnessesService(persistence, registry, broker, _now, runtime)
    svc._started = True  # type: ignore[attr-defined]
    svc._worker_id = "e2e-p8"  # type: ignore[attr-defined]
    asgi_mod._service = svc  # type: ignore[attr-defined]

    asyncio.run(broker.start())
    yield svc
    asyncio.run(broker.stop())
    reset_service_for_tests()


def _create_probed_harness(client: Client, header: str, name: str, kind: str) -> str:
    created = _json(
        client,
        "post",
        "/api/v1/harnesses",
        {"name": name, "configuration": {"kind": kind, "working_directory": "/tmp"}},
        HTTP_AUTHORIZATION=header,
    )
    assert created.status_code == 201, created.content
    harness_id = created.json()["id"]
    probed = client.post(f"/api/v1/harnesses/{harness_id}/probe", HTTP_AUTHORIZATION=header)
    assert probed.status_code == 200, probed.content
    return str(harness_id)


def _run_switch_worker(service: TalkToHarnessesService) -> None:
    async def _run() -> None:
        persistence = service._persistence  # pyright: ignore[reportPrivateUsage]
        claimed = await persistence.claim_commands("e2e-p8", 8)
        command = next(c for c in claimed if c.kind is CommandKind.SWITCH_HARNESS)
        service.processor._worker_id = "e2e-p8"  # pyright: ignore[reportPrivateUsage]
        await service.processor._execute_command(command)  # pyright: ignore[reportPrivateUsage]

    asyncio.run(_run())


@pytest.mark.django_db(transaction=True)
def test_phase8_switch_a_to_b_and_back_keeps_one_conversation(
    service: TalkToHarnessesService, user: Any
) -> None:
    header = f"Bearer {issue_token_sync(user).token}"
    owner = owner_id_for_user(user)
    client = Client()

    harness_a = _create_probed_harness(client, header, "a", "grok")
    harness_b = _create_probed_harness(client, header, "b", "codex")
    conv = _json(
        client,
        "post",
        "/api/v1/conversations",
        {"harness_id": harness_a, "title": "switching"},
        HTTP_AUTHORIZATION=header,
    )
    assert conv.status_code == 201, conv.content
    cid = conv.json()["detail"]["conversation"]["id"]

    def _binding() -> tuple[str, str | None]:
        async def _read() -> tuple[str, str | None]:
            state = await service._persistence.get_worker_snapshot(cid)  # pyright: ignore[reportPrivateUsage]
            assert state.binding is not None
            return str(state.binding.id), state.binding.native_session_id

        return asyncio.run(_read())

    first_a_binding, _ = _binding()

    switch = client.post(
        f"/api/v1/conversations/{cid}/switch",
        data=json.dumps({"harness_id": harness_b}),
        content_type="application/json",
        HTTP_AUTHORIZATION=header,
        HTTP_IDEMPOTENCY_KEY="to-b",
    )
    assert switch.status_code == 202, switch.content
    _run_switch_worker(service)

    detail = _json(client, "get", f"/api/v1/conversations/{cid}", HTTP_AUTHORIZATION=header)
    assert detail.status_code == 200, detail.content
    assert detail.json()["detail"]["harness_kind"] == "codex"
    b_binding, b_native = _binding()
    assert b_binding != first_a_binding

    back = client.post(
        f"/api/v1/conversations/{cid}/switch",
        data=json.dumps({"harness_id": harness_a}),
        content_type="application/json",
        HTTP_AUTHORIZATION=header,
        HTTP_IDEMPOTENCY_KEY="back-to-a",
    )
    assert back.status_code == 202, back.content
    _run_switch_worker(service)

    second_a_binding, second_a_native = _binding()
    # Switching back never resumes the previous binding or its native session.
    assert second_a_binding not in {first_a_binding, b_binding}
    assert second_a_native is not None
    assert second_a_native != b_native

    listed = _json(client, "get", "/api/v1/conversations", HTTP_AUTHORIZATION=header)
    assert [item["id"] for item in listed.json()["items"]] == [cid]
    assert owner

    events = _json(
        client,
        "get",
        f"/api/v1/conversations/{cid}/turns",
        HTTP_AUTHORIZATION=header,
    )
    assert events.status_code == 200
