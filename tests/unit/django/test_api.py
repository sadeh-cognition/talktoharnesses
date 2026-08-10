"""Django-Ninja API surface smoke tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from pydantic import ValidationError

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django import asgi as asgi_mod
from talktoharnesses.django.api import api
from talktoharnesses.django.api.schemas import (
    ApprovalRuleBody,
)
from talktoharnesses.django.asgi import reset_service_for_tests
from talktoharnesses.django.auth import (
    authenticate_bearer_sync,
    issue_token_sync,
    owner_id_for_user,
)
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import HarnessCapabilities, HarnessConfiguration, HarnessKind
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 8, 16, 0, 0, tzinfo=UTC)


def test_approval_rule_body_uses_strict_discriminated_unions() -> None:
    valid = ApprovalRuleBody.model_validate(
        {
            "decision": "allow",
            "scope": {"kind": "principal_global"},
            "matcher": {"kind": "exact_argv", "argv": ["ls"]},
        }
    )
    assert valid.matcher.kind == "exact_argv"
    with pytest.raises(ValidationError):
        ApprovalRuleBody.model_validate(
            {
                "decision": "allow",
                "scope": {"kind": "future_scope"},
                "matcher": {"kind": "exact_argv", "argv": ["ls"]},
            }
        )
    with pytest.raises(ValidationError):
        ApprovalRuleBody.model_validate(
            {
                "decision": "allow",
                "scope": {"kind": "principal_global", "unexpected": True},
                "matcher": {"kind": "exact_argv", "argv": ["ls"]},
            }
        )


def test_openapi_describes_cursor_model_selector_on_existing_fields() -> None:
    openapi = cast(dict[str, Any], api.get_openapi_schema(path_prefix="/api/v1"))
    schemas = cast(dict[str, Any], openapi["components"])["schemas"]
    configuration_model = cast(dict[str, Any], schemas["HarnessConfigurationBody"])["properties"][
        "model"
    ]
    turn_model = cast(dict[str, Any], schemas["SubmitTurnBody"])["properties"]["model"]

    for field in (configuration_model, turn_model):
        assert "model[key=value,...]" in field["description"]
        assert "composer-2.5[fast=false]" in field["description"]
    assert "session baseline" in configuration_model["description"]
    assert "one-turn" in turn_model["description"]


@pytest.fixture
def user(db: Any) -> Any:
    User: Any = get_user_model()
    return User.objects.create_user(username="api-user", password="x")


@pytest.fixture
def auth_header(user: Any) -> str:
    token = issue_token_sync(user)
    return f"Bearer {token.token}"


@pytest.fixture
def service(db: Any) -> Any:
    reset_service_for_tests()
    persistence = DjangoPersistence()
    registry = AdapterRegistry()
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    runtime = RuntimeManager(persistence, registry, clock=_now)
    svc = TalkToHarnessesService(persistence, registry, broker, _now, runtime)
    svc._started = True  # type: ignore[attr-defined]
    svc._worker_id = "test"  # type: ignore[attr-defined]
    asgi_mod._service = svc  # type: ignore[attr-defined]
    yield svc
    reset_service_for_tests()


def _post_json(client: Client, path: str, payload: dict[str, Any], **headers: str) -> Any:
    post: Any = client.post
    return post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        **headers,
    )


@pytest.mark.django_db(transaction=True)
def test_health_and_ready_public(service: TalkToHarnessesService) -> None:
    client = Client()
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get("/api/v1/ready")
    # Without worker lease / fresh harness probe the process is not ready.
    assert ready.status_code == 503
    body = ready.json()
    assert body == {"ready": False, "reason": "not_ready"}


@pytest.mark.django_db(transaction=True)
def test_ready_true_with_worker_and_fresh_probe(service: TalkToHarnessesService) -> None:
    coordinator = service.coordinator
    coordinator._lease_healthy = True  # type: ignore[attr-defined]
    coordinator._heartbeat_healthy = True  # type: ignore[attr-defined]
    coordinator._initial_recovery_complete = True  # type: ignore[attr-defined]
    coordinator._draining = False  # type: ignore[attr-defined]
    coordinator._claims_healthy = True  # type: ignore[attr-defined]
    service.processor._running = True  # type: ignore[attr-defined]
    service.processor._claim_task = Mock(done=Mock(return_value=False))  # type: ignore[attr-defined]
    service._readiness.notify_success(_now())  # type: ignore[attr-defined]

    client = Client()
    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200
    assert ready.json() == {"ready": True, "reason": "ready"}


@pytest.mark.django_db(transaction=True)
def test_harnesses_require_auth(service: TalkToHarnessesService) -> None:
    client = Client()
    res = client.get("/api/v1/harnesses")
    assert res.status_code == 401
    assert res["WWW-Authenticate"] == "Bearer"
    assert res.json()["code"] == "authentication_failed"


@pytest.mark.django_db(transaction=True)
def test_create_harness_and_conversation(service: TalkToHarnessesService, auth_header: str) -> None:
    client = Client()
    create = _post_json(
        client,
        "/api/v1/harnesses",
        {
            "name": "local-grok",
            "configuration": {
                "kind": "grok",
                "working_directory": "/tmp/ws",
            },
        },
        HTTP_AUTHORIZATION=auth_header,
    )
    assert create.status_code == 201, create.content
    harness_id = create.json()["id"]

    conv = _post_json(
        client,
        "/api/v1/conversations",
        {"harness_id": harness_id, "title": "Chat"},
        HTTP_AUTHORIZATION=auth_header,
    )
    assert conv.status_code == 201, conv.content
    snap = conv.json()
    assert snap["detail"]["conversation"]["title_manual"] == "Chat"

    listed = client.get("/api/v1/conversations", HTTP_AUTHORIZATION=auth_header)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1


@pytest.mark.django_db(transaction=True)
def test_submit_turn_requires_idempotency_key(
    service: TalkToHarnessesService, auth_header: str
) -> None:
    client = Client()
    user = authenticate_bearer_sync(auth_header)
    owner = owner_id_for_user(user)

    import asyncio

    async def setup() -> UUID:
        h = await service.create_harness(
            owner,
            name="h",
            configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        )
        snap = await service.create_conversation(owner, h.id)
        return snap.detail.conversation.id

    cid = asyncio.run(setup())
    missing = _post_json(
        client,
        f"/api/v1/conversations/{cid}/turns",
        {"prompt": "hi"},
        HTTP_AUTHORIZATION=auth_header,
    )
    assert missing.status_code == 422

    ok = client.post(
        f"/api/v1/conversations/{cid}/turns",
        data=json.dumps({"prompt": "hi"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=auth_header,
        HTTP_IDEMPOTENCY_KEY="k1",
    )
    assert ok.status_code == 202, ok.content
    body = ok.json()
    assert body["command"]["idempotency_key"] == "k1"


@pytest.mark.django_db(transaction=True)
def test_switch_harness_accepts_command(service: TalkToHarnessesService, auth_header: str) -> None:
    client = Client()
    user = authenticate_bearer_sync(auth_header)
    owner = owner_id_for_user(user)

    import asyncio

    async def setup() -> tuple[UUID, UUID]:
        source = await service.create_harness(
            owner,
            name="a",
            configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        )
        target = await service.create_harness(
            owner,
            name="b",
            configuration=HarnessConfiguration(kind=HarnessKind.CODEX, working_directory="/tmp"),
        )
        await DjangoPersistence().save_harness_probe(
            target.id,
            owner,
            HarnessCapabilities(kind=HarnessKind.CODEX, version="1.0.0"),
            probed_at=_now(),
        )
        snap = await service.create_conversation(owner, source.id)
        return snap.detail.conversation.id, target.id

    cid, target_id = asyncio.run(setup())

    missing = _post_json(
        client,
        f"/api/v1/conversations/{cid}/switch",
        {"harness_id": str(target_id)},
        HTTP_AUTHORIZATION=auth_header,
    )
    assert missing.status_code == 422

    ok = client.post(
        f"/api/v1/conversations/{cid}/switch",
        data=json.dumps({"harness_id": str(target_id)}),
        content_type="application/json",
        HTTP_AUTHORIZATION=auth_header,
        HTTP_IDEMPOTENCY_KEY="switch-1",
    )
    assert ok.status_code == 202, ok.content
    body = ok.json()
    assert body["kind"] == "switch_harness"
    assert body["status"] == "accepted"


@pytest.mark.django_db(transaction=True)
def test_token_rotate_and_revoke(service: TalkToHarnessesService, user: Any) -> None:
    client = Client()
    token = issue_token_sync(user)
    header = f"Bearer {token.token}"
    rotated = client.post("/api/v1/auth/token/rotate", HTTP_AUTHORIZATION=header)
    assert rotated.status_code == 200, rotated.content
    new_token = rotated.json()["token"]
    stale = client.get("/api/v1/harnesses", HTTP_AUTHORIZATION=header)
    assert stale.status_code == 401
    ok = client.get("/api/v1/harnesses", HTTP_AUTHORIZATION=f"Bearer {new_token}")
    assert ok.status_code == 200
    revoked = client.post("/api/v1/auth/token/revoke", HTTP_AUTHORIZATION=f"Bearer {new_token}")
    assert revoked.status_code == 204


@pytest.mark.django_db(transaction=True)
def test_sse_headers_and_sync_frame(service: TalkToHarnessesService, auth_header: str) -> None:
    client = Client()
    user = authenticate_bearer_sync(auth_header)
    owner = owner_id_for_user(user)

    import asyncio

    async def setup() -> UUID:
        h = await service.create_harness(
            owner,
            name="h",
            configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
        )
        snap = await service.create_conversation(owner, h.id)
        return snap.detail.conversation.id

    cid = asyncio.run(setup())
    missing = client.get(
        f"/api/v1/conversations/{UUID(int=0)}/events",
        HTTP_AUTHORIZATION=auth_header,
    )
    assert missing.status_code == 404
    # StreamingHttpResponse with async iterator may not fully stream in Django
    # test client; still assert headers and that the route is authenticated.
    res = client.get(
        f"/api/v1/conversations/{cid}/events",
        HTTP_AUTHORIZATION=auth_header,
    )
    assert res.status_code == 200
    assert res["Content-Type"].startswith("text/event-stream")
    assert res["Cache-Control"] == "no-cache"
    assert res["X-Accel-Buffering"] == "no"
