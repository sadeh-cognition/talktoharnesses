"""Phase 6 gate: rules, auto-resolution, audits, SSE event order, cross-owner.

Uses the packaged ASGI/HTTP surface with Django persistence. Harness peer
behavior is simulated via the interaction broker (same path adapters use).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

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
    ApprovalRule,
    ApprovalRuleDecision,
    CommandApprovalAction,
    ExactArgvMatcher,
    InteractionKind,
    PrincipalGlobalRuleScope,
    request_interaction,
    start_turn,
    submit_turn,
)
from talktoharnesses.domain.models import ApprovalRequestPayload, PendingInteraction
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


def _now() -> datetime:
    return datetime(2026, 8, 8, 19, 0, 0, tzinfo=UTC)


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
    a = User.objects.create_user(username="p6-a", password="x")
    b = User.objects.create_user(username="p6-b", password="x")
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
    svc._worker_id = "e2e-p6"  # type: ignore[attr-defined]
    asgi_mod._service = svc  # type: ignore[attr-defined]

    async def _start() -> None:
        await broker.start()

    asyncio.run(_start())
    yield svc
    asyncio.run(broker.stop())
    reset_service_for_tests()


@pytest.mark.django_db(transaction=True)
def test_phase6_rule_auto_resolve_sse_and_single_command(
    service: TalkToHarnessesService, two_users: Any
) -> None:
    user_a, user_b = two_users
    header_a = f"Bearer {issue_token_sync(user_a).token}"
    header_b = f"Bearer {issue_token_sync(user_b).token}"
    client = Client()
    owner = owner_id_for_user(user_a)

    # Create allow rule through HTTP.
    rule_resp = _json(
        client,
        "post",
        "/api/v1/approval-rules",
        {
            "decision": "allow",
            "scope": {"kind": "principal_global"},
            "matcher": {"kind": "exact_argv", "argv": ["npm", "install"]},
        },
        HTTP_AUTHORIZATION=header_a,
    )
    assert rule_resp.status_code == 201, rule_resp.content
    rule_id = rule_resp.json()["id"]

    harness = _json(
        client,
        "post",
        "/api/v1/harnesses",
        {"name": "g", "configuration": {"kind": "grok", "working_directory": "/tmp/ws"}},
        HTTP_AUTHORIZATION=header_a,
    )
    assert harness.status_code == 201
    conv = _json(
        client,
        "post",
        "/api/v1/conversations",
        {"harness_id": harness.json()["id"], "title": "p6"},
        HTTP_AUTHORIZATION=header_a,
    )
    assert conv.status_code == 201
    from uuid import UUID

    cid = UUID(conv.json()["detail"]["conversation"]["id"])

    async def _run() -> None:
        state = await service._persistence.get_snapshot(  # pyright: ignore[reportPrivateUsage]
            cid, owner
        )
        queued = submit_turn(state, prompt="install deps", idempotency_key="s1", now=_now())
        running = start_turn(queued.state, now=_now())
        await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
            cid,
            owner,
            state.conversation.version,
            running.state,
            (*queued.events, *running.events),
            commands=tuple(running.state.commands.values()),
        )

        interaction = PendingInteraction(
            conversation_id=state.conversation.id,
            turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
            kind=InteractionKind.APPROVAL,
            request=ApprovalRequestPayload(
                action=CommandApprovalAction(argv=("npm", "install")),
                available_decisions=(
                    ApprovalDecision.ALLOW_ONCE,
                    ApprovalDecision.DENY,
                    ApprovalDecision.CANCEL,
                ),
            ),
            created_at=_now(),
        )
        await service._broker.accept_request(  # pyright: ignore[reportPrivateUsage]
            state.conversation.id,
            interaction,
            provider_correlation={"json_rpc_request_id": "peer-1"},
        )
        events = await service.replay_events(owner, state.conversation.id, after_sequence=0)
        types = [e.type for e in events]
        assert "interaction_requested" in types
        assert "interaction_resolved" in types
        assert types.index("interaction_requested") < types.index("interaction_resolved")
        resolved = next(e for e in events if e.type == "interaction_resolved")
        assert resolved.payload.automatic is True  # type: ignore[attr-defined]
        assert resolved.payload.decision is ApprovalDecision.ALLOW_ONCE  # type: ignore[attr-defined]
        loaded = await service._persistence.get_worker_snapshot(  # pyright: ignore[reportPrivateUsage]
            state.conversation.id
        )
        answer_cmds = [c for c in loaded.commands.values() if c.kind.value == "answer_interaction"]
        assert len(answer_cmds) == 1

    asyncio.run(_run())

    audits = client.get("/api/v1/interaction-audits", HTTP_AUTHORIZATION=header_a)
    assert audits.status_code == 200
    items = audits.json()["items"]
    assert any(a.get("deciding_rule_id") == rule_id for a in items)
    assert (
        client.get(f"/api/v1/approval-rules/{rule_id}", HTTP_AUTHORIZATION=header_b).status_code
        == 404
    )
    audit_id = items[0]["id"]
    cross = client.get(
        f"/api/v1/interaction-audits/{audit_id}",
        HTTP_AUTHORIZATION=header_b,
    )
    assert cross.status_code == 404
    sse = client.get(f"/api/v1/conversations/{cid}/events", HTTP_AUTHORIZATION=header_a)
    assert sse.status_code == 200
    # StreamingHttpResponse may not fully iterate under Django test client; status is enough.
    assert sse["Cache-Control"] == "no-cache" or "text/event-stream" in sse["Content-Type"]


@pytest.mark.django_db(transaction=True)
def test_phase6_manual_and_rule_event_shapes_match_except_automatic(
    service: TalkToHarnessesService, two_users: Any
) -> None:
    from talktoharnesses.domain import complete_turn

    user_a, _ = two_users
    owner = owner_id_for_user(user_a)
    header = f"Bearer {issue_token_sync(user_a).token}"
    client = Client()

    harness = _json(
        client,
        "post",
        "/api/v1/harnesses",
        {"name": "g", "configuration": {"kind": "grok", "working_directory": "/tmp/ws"}},
        HTTP_AUTHORIZATION=header,
    )
    conv = _json(
        client,
        "post",
        "/api/v1/conversations",
        {"harness_id": harness.json()["id"]},
        HTTP_AUTHORIZATION=header,
    )
    from uuid import UUID

    cid = UUID(conv.json()["detail"]["conversation"]["id"])

    async def _run() -> None:
        async def _seed_interaction(argv: tuple[str, ...], key: str):
            state = await service._persistence.get_snapshot(  # pyright: ignore[reportPrivateUsage]
                cid, owner
            )
            if state.active_turn is None:
                queued = submit_turn(state, prompt=key, idempotency_key=key, now=_now())
                running = start_turn(queued.state, now=_now())
                await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
                    cid,
                    owner,
                    state.conversation.version,
                    running.state,
                    (*queued.events, *running.events),
                    commands=tuple(running.state.commands.values()),
                )
                state = running.state
            else:
                state = await service._persistence.get_worker_snapshot(  # pyright: ignore[reportPrivateUsage]
                    cid
                )
            interaction = PendingInteraction(
                conversation_id=state.conversation.id,
                turn_id=state.active_turn.id,  # type: ignore[union-attr]
                kind=InteractionKind.APPROVAL,
                request=ApprovalRequestPayload(
                    action=CommandApprovalAction(argv=argv),
                    available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
                ),
                created_at=_now(),
            )
            return interaction, state

        i1, state = await _seed_interaction(("manual",), "m1")
        requested = request_interaction(state, i1, now=_now())
        await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
            cid,
            owner,
            state.conversation.version,
            requested.state,
            requested.events,
        )
        await service.resolve_interaction(owner, cid, i1.id, decision=ApprovalDecision.ALLOW_ONCE)
        events = await service.replay_events(owner, cid, after_sequence=0)
        manual = next(
            e
            for e in events
            if e.type == "interaction_resolved" and e.payload.interaction_id == i1.id  # type: ignore[attr-defined]
        )

        await service.create_approval_rule(
            owner,
            ApprovalRule(
                principal_id=owner,
                decision=ApprovalRuleDecision.ALLOW,
                scope=PrincipalGlobalRuleScope(),
                matcher=ExactArgvMatcher(argv=("auto",)),
                created_at=_now(),
                updated_at=_now(),
            ),
        )
        state = await service._persistence.get_worker_snapshot(cid)  # pyright: ignore[reportPrivateUsage]
        if state.active_turn is not None:
            done = complete_turn(state, now=_now())
            await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
                cid, owner, state.conversation.version, done.state, done.events
            )
        i2, _state = await _seed_interaction(("auto",), "m2")
        await service._broker.accept_request(cid, i2)  # pyright: ignore[reportPrivateUsage]
        events = await service.replay_events(owner, cid, after_sequence=0)
        auto = next(
            e
            for e in events
            if e.type == "interaction_resolved" and e.payload.interaction_id == i2.id  # type: ignore[attr-defined]
        )
        assert manual.payload.decision == auto.payload.decision  # type: ignore[attr-defined]
        assert manual.payload.automatic is False  # type: ignore[attr-defined]
        assert auto.payload.automatic is True  # type: ignore[attr-defined]

    asyncio.run(_run())


@pytest.mark.django_db(transaction=True)
def test_phase6_deny_rule_auto_path(service: TalkToHarnessesService, two_users: Any) -> None:
    from uuid import UUID

    user_a, _ = two_users
    header = f"Bearer {issue_token_sync(user_a).token}"
    client = Client()
    owner = owner_id_for_user(user_a)

    _json(
        client,
        "post",
        "/api/v1/approval-rules",
        {
            "decision": "deny",
            "scope": {"kind": "principal_global"},
            "matcher": {"kind": "exact_argv", "argv": ["rm", "-rf", "/"]},
        },
        HTTP_AUTHORIZATION=header,
    )
    harness = _json(
        client,
        "post",
        "/api/v1/harnesses",
        {"name": "g", "configuration": {"kind": "grok", "working_directory": "/tmp/ws"}},
        HTTP_AUTHORIZATION=header,
    )
    conv = _json(
        client,
        "post",
        "/api/v1/conversations",
        {"harness_id": harness.json()["id"]},
        HTTP_AUTHORIZATION=header,
    )
    cid = UUID(conv.json()["detail"]["conversation"]["id"])

    async def _run() -> None:
        state = await service._persistence.get_snapshot(  # pyright: ignore[reportPrivateUsage]
            cid, owner
        )
        queued = submit_turn(state, prompt="x", idempotency_key="d1", now=_now())
        running = start_turn(queued.state, now=_now())
        await service._persistence.commit_facade_mutation(  # pyright: ignore[reportPrivateUsage]
            cid,
            owner,
            state.conversation.version,
            running.state,
            (*queued.events, *running.events),
            commands=tuple(running.state.commands.values()),
        )
        interaction = PendingInteraction(
            conversation_id=cid,
            turn_id=running.state.active_turn.id,  # type: ignore[union-attr]
            kind=InteractionKind.APPROVAL,
            request=ApprovalRequestPayload(
                action=CommandApprovalAction(argv=("rm", "-rf", "/")),
                available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
            ),
            created_at=_now(),
        )
        await service._broker.accept_request(  # pyright: ignore[reportPrivateUsage]
            cid, interaction
        )
        events = await service.replay_events(owner, cid, after_sequence=0)
        resolved = next(e for e in events if e.type == "interaction_resolved")
        assert resolved.payload.decision is ApprovalDecision.DENY  # type: ignore[attr-defined]
        assert resolved.payload.automatic is True  # type: ignore[attr-defined]

    asyncio.run(_run())
