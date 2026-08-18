"""HTTP approval-rule and interaction-audit surface (Phase 6 WP4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.django import asgi as asgi_mod
from talktoharnesses.django.asgi import reset_service_for_tests
from talktoharnesses.django.auth import issue_token_sync, owner_id_for_user
from talktoharnesses.django.persistence import DjangoPersistence
from talktoharnesses.domain import (
    ApprovalDecision,
    InteractionKind,
    request_interaction,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.models import ApprovalRequestPayload, PendingInteraction
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 8, 16, 30, 0, tzinfo=UTC)


@pytest.fixture
def two_users(db: Any) -> tuple[Any, Any]:
    User: Any = get_user_model()
    a = User.objects.create_user(username="rule-a", password="x")
    b = User.objects.create_user(username="rule-b", password="x")
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
    svc._worker_id = "test"  # type: ignore[attr-defined]
    asgi_mod._service = svc  # type: ignore[attr-defined]
    yield svc
    reset_service_for_tests()


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


def _auth(user: Any) -> str:
    return f"Bearer {issue_token_sync(user).token}"


_RULE_BODY = {
    "decision": "allow",
    "scope": {"kind": "principal_global"},
    "matcher": {"kind": "exact_argv", "argv": ["ls", "-la"]},
}


@pytest.mark.django_db(transaction=True)
def test_approval_rule_crud_and_cursors(service: TalkToHarnessesService, two_users: Any) -> None:
    user_a, _user_b = two_users
    header = _auth(user_a)
    client = Client()

    created = _json(client, "post", "/api/v1/approval-rules", _RULE_BODY, HTTP_AUTHORIZATION=header)
    assert created.status_code == 201, created.content
    rule_id = created.json()["id"]
    assert created.json()["decision"] == "allow"
    assert created.json()["matcher"]["argv"] == ["ls", "-la"]

    got = client.get(f"/api/v1/approval-rules/{rule_id}", HTTP_AUTHORIZATION=header)
    assert got.status_code == 200
    assert got.json()["id"] == rule_id

    listed = client.get("/api/v1/approval-rules", HTTP_AUTHORIZATION=header)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    replaced = _json(
        client,
        "put",
        f"/api/v1/approval-rules/{rule_id}",
        {
            "decision": "deny",
            "scope": {"kind": "principal_global"},
            "matcher": {"kind": "exact_argv", "argv": ["rm"]},
        },
        HTTP_AUTHORIZATION=header,
    )
    assert replaced.status_code == 200, replaced.content
    assert replaced.json()["decision"] == "deny"

    deleted = client.delete(f"/api/v1/approval-rules/{rule_id}", HTTP_AUTHORIZATION=header)
    assert deleted.status_code == 204
    missing = client.get(f"/api/v1/approval-rules/{rule_id}", HTTP_AUTHORIZATION=header)
    assert missing.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_rule_and_audit_cross_owner_are_404(
    service: TalkToHarnessesService, two_users: Any
) -> None:
    user_a, user_b = two_users
    header_a = _auth(user_a)
    header_b = _auth(user_b)
    client = Client()

    created = _json(
        client, "post", "/api/v1/approval-rules", _RULE_BODY, HTTP_AUTHORIZATION=header_a
    )
    assert created.status_code == 201
    rule_id = created.json()["id"]

    assert (
        client.get(f"/api/v1/approval-rules/{rule_id}", HTTP_AUTHORIZATION=header_b).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/approval-rules/{rule_id}", HTTP_AUTHORIZATION=header_b).status_code
        == 404
    )
    # Unknown id same as cross-owner.
    assert (
        client.get(f"/api/v1/approval-rules/{uuid4()}", HTTP_AUTHORIZATION=header_a).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/interaction-audits/{uuid4()}", HTTP_AUTHORIZATION=header_a).status_code
        == 404
    )


@pytest.mark.django_db(transaction=True)
def test_resolve_create_and_allow_and_audit_list(
    service: TalkToHarnessesService, two_users: Any
) -> None:
    import asyncio

    from talktoharnesses.domain.models import CommandApprovalAction

    user_a, user_b = two_users
    header_a = _auth(user_a)
    header_b = _auth(user_b)
    client = Client()
    owner = owner_id_for_user(user_a)

    harness = _json(
        client,
        "post",
        "/api/v1/harnesses",
        {"name": "h", "configuration": {"kind": "grok", "working_directory": "/tmp/ws"}},
        HTTP_AUTHORIZATION=header_a,
    )
    assert harness.status_code == 201
    conv = _json(
        client,
        "post",
        "/api/v1/conversations",
        {"harness_id": harness.json()["id"]},
        HTTP_AUTHORIZATION=header_a,
    )
    assert conv.status_code == 201
    from uuid import UUID

    cid = UUID(conv.json()["detail"]["conversation"]["id"])

    async def _seed() -> Any:
        state = await service._persistence.get_snapshot(  # pyright: ignore[reportPrivateUsage]
            cid, owner
        )
        queued = submit_turn(state, prompt="x", idempotency_key="k1", now=_now())
        running = start_turn(queued.state, now=_now())
        interaction = PendingInteraction(
            conversation_id=state.conversation.id,
            turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
            kind=InteractionKind.APPROVAL,
            request=ApprovalRequestPayload(
                action=CommandApprovalAction(argv=("ls", "-la")),
                available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
            ),
            created_at=_now(),
        )
        requested = request_interaction(running.state, interaction, now=_now())
        await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
            state.conversation.id,
            owner,
            state.conversation.version,
            requested.state,
            (*queued.events, *running.events, *requested.events),
        )
        return interaction

    interaction = asyncio.run(_seed())

    resolve = _json(
        client,
        "post",
        f"/api/v1/conversations/{cid}/interactions/{interaction.id}/resolve",
        {
            "decision": "allow_once",
            "create_rule": {
                "decision": "allow",
                "scope": {"kind": "principal_global"},
                "matcher": {"kind": "exact_argv", "argv": ["ls", "-la"]},
            },
        },
        HTTP_AUTHORIZATION=header_a,
    )
    assert resolve.status_code == 202, resolve.content
    cmd = resolve.json()
    assert cmd["kind"] == "answer_interaction"

    again = _json(
        client,
        "post",
        f"/api/v1/conversations/{cid}/interactions/{interaction.id}/resolve",
        {"decision": "deny"},
        HTTP_AUTHORIZATION=header_a,
    )
    assert again.status_code == 202
    assert again.json()["id"] == cmd["id"]

    audits = client.get("/api/v1/interaction-audits", HTTP_AUTHORIZATION=header_a)
    assert audits.status_code == 200
    items = audits.json()["items"]
    assert len(items) >= 1
    audit_id = items[0]["id"]
    one = client.get(f"/api/v1/interaction-audits/{audit_id}", HTTP_AUTHORIZATION=header_a)
    assert one.status_code == 200
    cross = client.get(
        f"/api/v1/interaction-audits/{audit_id}",
        HTTP_AUTHORIZATION=header_b,
    )
    assert cross.status_code == 404
    body = one.json()
    assert "provider_correlation" not in body


@pytest.mark.django_db(transaction=True)
def test_rule_validation_422(service: TalkToHarnessesService, two_users: Any) -> None:
    user_a, _ = two_users
    header = _auth(user_a)
    client = Client()
    bad = _json(
        client,
        "post",
        "/api/v1/approval-rules",
        {
            "decision": "allow",
            "scope": {"kind": "not_a_scope"},
            "matcher": {"kind": "exact_argv", "argv": ["x"]},
        },
        HTTP_AUTHORIZATION=header,
    )
    assert bad.status_code == 422


@pytest.mark.django_db(transaction=True)
def test_draft_update_and_all_decisions_and_structured_question(
    service: TalkToHarnessesService, two_users: Any
) -> None:
    import asyncio
    from uuid import UUID

    from talktoharnesses.domain.models import CanonicalQuestion, StructuredQuestionPayload

    user_a, _ = two_users
    header = _auth(user_a)
    client = Client()
    owner = owner_id_for_user(user_a)

    harness = _json(
        client,
        "post",
        "/api/v1/harnesses",
        {"name": "h", "configuration": {"kind": "grok", "working_directory": "/tmp/ws"}},
        HTTP_AUTHORIZATION=header,
    )
    harness_id = harness.json()["id"]

    def _new_conversation() -> UUID:
        conv = _json(
            client,
            "post",
            "/api/v1/conversations",
            {"harness_id": harness_id},
            HTTP_AUTHORIZATION=header,
        )
        assert conv.status_code == 201, conv.content
        return UUID(conv.json()["detail"]["conversation"]["id"])

    async def _seed_approval(cid: UUID, key: str) -> Any:
        state = await service._persistence.get_snapshot(  # pyright: ignore[reportPrivateUsage]
            cid, owner
        )
        queued = submit_turn(state, prompt=key, idempotency_key=key, now=_now())
        running = start_turn(queued.state, now=_now())
        interaction = PendingInteraction(
            conversation_id=cid,
            turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
            kind=InteractionKind.APPROVAL,
            request=ApprovalRequestPayload(available_decisions=tuple(ApprovalDecision)),
            created_at=_now(),
        )
        requested = request_interaction(running.state, interaction, now=_now())
        await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
            cid,
            owner,
            state.conversation.version,
            requested.state,
            (*queued.events, *running.events, *requested.events),
        )
        return interaction

    # Draft then resolve.
    cid = _new_conversation()
    interaction = asyncio.run(_seed_approval(cid, "draft-1"))
    draft = _json(
        client,
        "patch",
        f"/api/v1/conversations/{cid}/interactions/{interaction.id}/draft",
        {"draft": {"note": "maybe"}},
        HTTP_AUTHORIZATION=header,
    )
    assert draft.status_code == 200, draft.content
    assert draft.json()["draft"] == {"note": "maybe"}
    assert "provider_correlation" not in draft.json()
    resp = _json(
        client,
        "post",
        f"/api/v1/conversations/{cid}/interactions/{interaction.id}/resolve",
        {"decision": "allow_session"},
        HTTP_AUTHORIZATION=header,
    )
    assert resp.status_code == 202, resp.content

    for decision in ("allow_once", "deny", "cancel"):
        cid = _new_conversation()
        inter = asyncio.run(_seed_approval(cid, f"k-{decision}"))
        resp = _json(
            client,
            "post",
            f"/api/v1/conversations/{cid}/interactions/{inter.id}/resolve",
            {"decision": decision},
            HTTP_AUTHORIZATION=header,
        )
        assert resp.status_code == 202, (decision, resp.content)
        assert resp.json()["kind"] == "answer_interaction"

    # Structured question.
    cid = _new_conversation()

    async def _seed_question() -> Any:
        state = await service._persistence.get_snapshot(  # pyright: ignore[reportPrivateUsage]
            cid, owner
        )
        queued = submit_turn(state, prompt="q", idempotency_key="q1", now=_now())
        running = start_turn(queued.state, now=_now())
        interaction = PendingInteraction(
            conversation_id=cid,
            turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
            kind=InteractionKind.STRUCTURED_QUESTION,
            request=StructuredQuestionPayload(
                questions=(CanonicalQuestion(id="q1", question="ok?"),)
            ),
            created_at=_now(),
        )
        requested = request_interaction(running.state, interaction, now=_now())
        await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
            cid,
            owner,
            state.conversation.version,
            requested.state,
            (*queued.events, *running.events, *requested.events),
        )
        return interaction

    q = asyncio.run(_seed_question())
    resolved = _json(
        client,
        "post",
        f"/api/v1/conversations/{cid}/interactions/{q.id}/resolve",
        {"answers": {"q1": "yes"}},
        HTTP_AUTHORIZATION=header,
    )
    assert resolved.status_code == 202, resolved.content
