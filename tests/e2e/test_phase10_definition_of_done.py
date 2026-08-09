"""Phase 10 definition-of-done journeys with deterministic fake adapters.

Native create/resume evidence remains the separate live gates.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

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
from talktoharnesses.domain.enums import (
    ApprovalDecision,
    ApprovalRuleDecision,
    HarnessKind,
    InteractionKind,
)
from talktoharnesses.domain.events import (
    InteractionRequestedPayload,
    TurnCompletedPayload,
    TurnInterruptedPayload,
)
from talktoharnesses.domain.models import (
    ApprovalRequestPayload,
    ApprovalRule,
    CommandApprovalAction,
    ExactArgvMatcher,
    HarnessConfiguration,
    InteractionAnswer,
    PrincipalGlobalRuleScope,
)
from talktoharnesses.providers.adapter import HarnessSession, TurnRequest
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


class _Phase10Adapter(FakeAdapter):
    sdk_managed = True
    kind = HarnessKind.GROK

    def __init__(self) -> None:
        super().__init__(seed_reply="silent")
        self._events: asyncio.Queue[Any | None] = asyncio.Queue()
        self._active_request: TurnRequest | None = None
        self._interaction_id: UUID | None = None
        self.answers: list[InteractionAnswer] = []
        self.start_calls = 0
        self.resume_calls = 0

    async def start(self, request: Any) -> HarnessSession:
        self.start_calls += 1
        return await super().start(request)

    async def resume(self, request: Any) -> HarnessSession:
        self.resume_calls += 1
        return await super().resume(request)

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        del session
        self.submissions.append(request)
        self._active_request = request
        if len(self.submissions) == 1:
            self._interaction_id = uuid4()
            await self._events.put(
                InteractionRequestedPayload(
                    turn_id=request.turn_id,
                    interaction_id=self._interaction_id,
                    kind=InteractionKind.APPROVAL,
                    request=ApprovalRequestPayload(
                        action=CommandApprovalAction(argv=("phase10",)),
                        available_decisions=(
                            ApprovalDecision.ALLOW_ONCE,
                            ApprovalDecision.DENY,
                        ),
                    ),
                )
            )

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        del session
        assert self._active_request is not None
        # Auto-policy may deliver an answer before the test helper resolves the
        # latest request; accept any answer for the active turn.
        self._interaction_id = answer.interaction_id
        self.answers.append(answer)
        await self._events.put(
            TurnCompletedPayload(
                turn_id=self._active_request.turn_id,
                terminal_reason="end_turn",
            )
        )

    async def interrupt(self, session: HarnessSession) -> None:
        del session
        assert self._active_request is not None
        self.interrupt_calls += 1
        await self._events.put(
            TurnInterruptedPayload(turn_id=self._active_request.turn_id, reason="requested")
        )

    def events(self, session: HarnessSession) -> AsyncIterator[Any]:
        del session

        async def _gen() -> AsyncIterator[Any]:
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        await super().close(session)
        await self._events.put(None)


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def two_users(db: Any) -> tuple[Any, Any]:
    User: Any = get_user_model()
    a = User.objects.create_user(username="p10-a", password="x")
    b = User.objects.create_user(username="p10-b", password="x")
    return a, b


@pytest.fixture
def service(db: Any) -> Any:
    reset_service_for_tests()
    FakeAdapter.instances.clear()
    persistence = DjangoPersistence()
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, _Phase10Adapter)  # type: ignore[arg-type]
    broker = InProcessCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    runtime = RuntimeManager(persistence, registry, clock=_now)
    svc = TalkToHarnessesService(persistence, registry, broker, _now, runtime)
    svc._started = True  # pyright: ignore[reportPrivateUsage]
    svc._worker_id = "p10"  # pyright: ignore[reportPrivateUsage]
    coordinator = svc.coordinator
    coordinator._lease_healthy = True  # pyright: ignore[reportPrivateUsage]
    coordinator._heartbeat_healthy = True  # pyright: ignore[reportPrivateUsage]
    coordinator._initial_recovery_complete = True  # pyright: ignore[reportPrivateUsage]
    coordinator._draining = False  # pyright: ignore[reportPrivateUsage]
    coordinator._claims_healthy = True  # pyright: ignore[reportPrivateUsage]
    svc.processor.initialize_worker("p10")
    svc.processor._running = True  # pyright: ignore[reportPrivateUsage]
    svc.processor._claims_enabled = True  # pyright: ignore[reportPrivateUsage]

    class _HealthyClaimTask:
        def done(self) -> bool:
            return False

    svc.processor._claim_task = _HealthyClaimTask()  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    svc._readiness.notify_success(_now())  # pyright: ignore[reportPrivateUsage]
    asgi_mod._service = svc  # pyright: ignore[reportPrivateUsage]

    async def _start_broker() -> None:
        await broker.start()

    asyncio.run(_start_broker())
    yield svc
    asyncio.run(runtime.shutdown())
    asyncio.run(broker.stop())
    reset_service_for_tests()


async def _execute_command(
    service: TalkToHarnessesService,
    *,
    conversation_id: UUID,
    command_id: UUID,
) -> None:
    """Claim and execute one accepted command through the real processor path."""
    persistence = service._persistence  # pyright: ignore[reportPrivateUsage]
    claimed = await persistence.claim_commands("p10", 8, lease_duration=30.0)
    selected = next(item for item in claimed if item.command.id == command_id)
    service.processor.set_fence(conversation_id, selected.fence)
    await asyncio.wait_for(
        service.processor._execute_command(selected.command),  # pyright: ignore[reportPrivateUsage]
        timeout=5.0,
    )


async def _wait_for_event(
    service: TalkToHarnessesService,
    *,
    owner_id: str,
    conversation_id: UUID,
    event_type: str,
) -> list[Any]:
    """Read committed history until the requested event appears."""

    for _ in range(100):
        events = list(await service.replay_events(owner_id, conversation_id, after_sequence=0))
        if any(event.type == event_type for event in events):
            assert [event.sequence for event in events] == sorted(
                event.sequence for event in events
            )
            return events
        await asyncio.sleep(0.01)
    raise AssertionError(f"executed command did not commit {event_type}")


async def _resolve_first_turn(
    service: TalkToHarnessesService,
    *,
    owner_id: str,
    conversation_id: UUID,
    command_id: UUID,
) -> list[Any]:
    """Exercise deferred approval, duplicate resolution, and successful completion."""
    await _execute_command(
        service,
        conversation_id=conversation_id,
        command_id=command_id,
    )
    requested = await _wait_for_event(
        service,
        owner_id=owner_id,
        conversation_id=conversation_id,
        event_type="interaction_requested",
    )
    request_event = next(
        event for event in reversed(requested) if event.type == "interaction_requested"
    )
    interaction_id = request_event.payload.interaction_id
    rule = ApprovalRule(
        principal_id=owner_id,
        decision=ApprovalRuleDecision.ALLOW,
        scope=PrincipalGlobalRuleScope(),
        matcher=ExactArgvMatcher(argv=("phase10",)),
        created_at=_now(),
        updated_at=_now(),
    )
    answer = await service.resolve_interaction(
        owner_id,
        conversation_id,
        interaction_id,
        decision=ApprovalDecision.ALLOW_ONCE,
        create_rule=rule,
    )
    duplicate = await service.resolve_interaction(
        owner_id,
        conversation_id,
        interaction_id,
        decision=ApprovalDecision.DENY,
    )
    assert duplicate.id == answer.id
    adapter = next(
        item
        for item in reversed(FakeAdapter.instances)
        if isinstance(item, _Phase10Adapter) and item.submissions
    )
    assert not adapter.answers, "provider delivery preceded committed resolution"
    resolved = await service.replay_events(owner_id, conversation_id, after_sequence=0)
    assert any(event.type == "interaction_resolved" for event in resolved)
    await _execute_command(
        service,
        conversation_id=conversation_id,
        command_id=answer.id,
    )
    completed = await _wait_for_event(
        service,
        owner_id=owner_id,
        conversation_id=conversation_id,
        event_type="turn_completed",
    )
    assert adapter.answers[0].decision is ApprovalDecision.ALLOW_ONCE
    saved = completed[len(completed) // 2].sequence
    suffix = await service.replay_events(owner_id, conversation_id, after_sequence=saved)
    assert [event.sequence for event in suffix] == list(
        range(saved + 1, completed[-1].sequence + 1)
    )
    return completed


async def _interrupt_next_turn(
    service: TalkToHarnessesService,
    *,
    owner_id: str,
    conversation_id: UUID,
    command_id: UUID,
) -> list[Any]:
    await _execute_command(
        service,
        conversation_id=conversation_id,
        command_id=command_id,
    )
    interrupted = await service.interrupt(
        owner_id,
        conversation_id,
        idempotency_key=str(uuid4()),
    )
    await _execute_command(
        service,
        conversation_id=conversation_id,
        command_id=interrupted.id,
    )
    return await _wait_for_event(
        service,
        owner_id=owner_id,
        conversation_id=conversation_id,
        event_type="turn_interrupted",
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_service_journey_owner_isolation(
    service: TalkToHarnessesService, two_users: tuple[Any, Any]
) -> None:
    user_a, user_b = two_users
    owner_a = owner_id_for_user(user_a)
    owner_b = owner_id_for_user(user_b)

    harness = await service.create_harness(
        owner_a,
        name="g1",
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp"),
    )
    probed = await service.probe_harness(owner_a, harness.id)
    assert probed.capabilities.version == "test-1"
    conv = await service.create_conversation(owner_a, harness.id)
    conversation_id = conv.detail.conversation.id
    page_b = await service.list_conversations(owner_b)
    assert all(item.id != conversation_id for item in page_b.items)

    submitted = await service.submit_turn(
        owner_a,
        conversation_id,
        prompt="hello phase10",
        idempotency_key=str(uuid4()),
    )
    assert submitted.command.id

    events = await _resolve_first_turn(
        service,
        owner_id=owner_a,
        conversation_id=conversation_id,
        command_id=submitted.command.id,
    )
    assert any(event.type == "turn_completed" for event in events)

    # Idempotent accept without provider delivery.
    again = await service.submit_turn(
        owner_a,
        conversation_id,
        prompt="hello phase10",
        idempotency_key=submitted.command.idempotency_key,
    )
    assert again.command.id == submitted.command.id

    next_turn = await service.submit_turn(
        owner_a,
        conversation_id,
        prompt="interrupt phase10",
        idempotency_key=str(uuid4()),
    )
    interrupted = await _interrupt_next_turn(
        service,
        owner_id=owner_a,
        conversation_id=conversation_id,
        command_id=next_turn.command.id,
    )
    assert any(event.type == "turn_interrupted" for event in interrupted)

    await service.processor.cancel_pump(conversation_id)
    await service._runtime.close(conversation_id, reason="phase10-restart")  # pyright: ignore[reportPrivateUsage]
    resumed_turn = await service.submit_turn(
        owner_a,
        conversation_id,
        prompt="resume phase10",
        idempotency_key=str(uuid4()),
    )
    await _resolve_first_turn(
        service,
        owner_id=owner_a,
        conversation_id=conversation_id,
        command_id=resumed_turn.command.id,
    )
    resumed_adapter = next(
        item
        for item in reversed(FakeAdapter.instances)
        if isinstance(item, _Phase10Adapter) and item.submissions
    )
    assert resumed_adapter.resume_calls == 1


@pytest.mark.django_db(transaction=True)
def test_http_sse_journey_owner_isolation(
    service: TalkToHarnessesService, two_users: tuple[Any, Any]
) -> None:
    user_a, user_b = two_users
    client = Client()
    token_a = issue_token_sync(user_a).token
    token_b = issue_token_sync(user_b).token
    header_a = f"Bearer {token_a}"
    header_b = f"Bearer {token_b}"

    create_h = client.post(
        "/api/v1/harnesses",
        data=json.dumps(
            {
                "name": "http-g1",
                "configuration": {"kind": "grok", "working_directory": "/tmp"},
            }
        ),
        content_type="application/json",
        HTTP_AUTHORIZATION=header_a,
    )
    assert create_h.status_code == 201, create_h.content
    harness_id = create_h.json()["id"]

    probe = client.post(
        f"/api/v1/harnesses/{harness_id}/probe",
        HTTP_AUTHORIZATION=header_a,
    )
    assert probe.status_code == 200, probe.content

    create_c = client.post(
        "/api/v1/conversations",
        data=json.dumps({"harness_id": harness_id, "title": "Phase10"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=header_a,
    )
    assert create_c.status_code == 201, create_c.content
    conversation_id = create_c.json()["detail"]["conversation"]["id"]

    other = client.get(
        f"/api/v1/conversations/{conversation_id}",
        HTTP_AUTHORIZATION=header_b,
    )
    assert other.status_code == 404

    submit = client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        data=json.dumps({"prompt": "http journey"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=header_a,
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert submit.status_code == 202, submit.content
    command_id = UUID(submit.json()["command"]["id"])

    async def _execute_and_read_sse() -> str:
        committed = await _resolve_first_turn(
            service,
            owner_id=owner_id_for_user(user_a),
            conversation_id=UUID(conversation_id),
            command_id=command_id,
        )
        assert any(event.type == "turn_completed" for event in committed)
        from talktoharnesses.django.api.sse import iter_sse

        stream = iter_sse(
            service,
            owner_id=owner_id_for_user(user_a),
            conversation_id=UUID(conversation_id),
            last_event_id=0,
        )
        try:
            frames: list[str] = []
            while True:
                frame = await anext(stream)
                assert isinstance(frame, str)
                frames.append(frame)
                if "event: turn_completed" in frame:
                    return "".join(frames)
        finally:
            from collections.abc import Awaitable, Callable
            from typing import cast

            close_stream = getattr(stream, "aclose", None)
            if callable(close_stream):
                await cast(Callable[[], Awaitable[object]], close_stream)()

    sse_body = asyncio.run(_execute_and_read_sse())
    assert "event: turn_completed" in sse_body

    events = client.get(
        f"/api/v1/conversations/{conversation_id}/events",
        HTTP_AUTHORIZATION=header_a,
    )
    assert events.status_code == 200
    assert events["Content-Type"].startswith("text/event-stream")
    events.close()

    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200
