"""Split startup failures terminate turns through the official client's SSE stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from tests.runtime.memory_persistence import MemoryPersistence
from tests.unit.remote.test_remote_adapter import (
    FakeSplit,
    _adapter,  # pyright: ignore[reportPrivateUsage]
)
from tth_types.split_api import SplitError

from talktoharnesses.application.broker import InProcessCommittedEventBroker
from talktoharnesses.application.command_processor import (
    _MAX_TRANSIENT_STARTUP_ATTEMPTS,
    CommandProcessor,
)
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.client import AsyncTalkToHarnessesClient
from talktoharnesses.django.api import sse
from talktoharnesses.domain import (
    CommandStatus,
    HarnessConfiguration,
    HarnessKind,
    TurnStatus,
    new_conversation_state,
    submit_turn,
)
from talktoharnesses.domain.events import ConversationEvent, TurnFailedPayload
from talktoharnesses.domain.models import ConversationHarnessBinding, SyncProjection
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager


class _SseBody(httpx.AsyncByteStream):
    def __init__(self, frames: AsyncIterator[str]) -> None:
        self.frames = frames

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for frame in self.frames:
            yield frame.encode()

    async def aclose(self) -> None:
        await self.frames.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("failure_path", ["/v1/probe", "/v1/sessions"])
@pytest.mark.parametrize(
    ("status", "code", "details", "message"),
    [
        (500, "internal_error", {}, "protocol error"),
        (
            409,
            "provider_incompatible",
            {"reason": "authentication_required"},
            "harness authentication failed; refresh the provider credentials on the TTH host",
        ),
    ],
)
async def test_startup_failure_reaches_live_and_reconnecting_client(
    monkeypatch: pytest.MonkeyPatch,
    resume: bool,
    failure_path: str,
    status: int,
    code: str,
    details: dict[str, str],
    message: str,
) -> None:
    class FailingSplit(FakeSplit):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == failure_path:
                return httpx.Response(
                    status,
                    content=SplitError(
                        code=code, message="private provider diagnostic", details=details
                    ).model_dump_json(),
                )
            return super().handler(request)

    now = datetime.now(UTC)
    state = new_conversation_state(owner_id="owner", now=now)
    conversation_id = state.conversation.id
    binding = ConversationHarnessBinding(
        conversation_id=conversation_id,
        kind=HarnessKind.GROK,
        configuration=HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/work"),
        native_session_id="native-1" if resume else None,
        created_at=now,
    )
    state = state.model_copy(
        update={
            "binding": binding,
            "conversation": state.conversation.model_copy(
                update={"current_binding_id": binding.id}
            ),
        }
    )
    submitted = submit_turn(state, prompt="hello", idempotency_key="startup", now=now)
    assert submitted.command is not None
    assert submitted.command.target_turn_id is not None
    persistence = MemoryPersistence()
    persistence.seed(submitted.state)
    await persistence.accept_command(submitted.command)
    split = FailingSplit(kind=HarnessKind.GROK)
    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: _adapter(split))
    broker = InProcessCommittedEventBroker()
    runtime = RuntimeManager(persistence, registry)
    service = TalkToHarnessesService(
        persistence, registry, broker, lambda: datetime.now(UTC), runtime
    )
    # Short leases so a transient (protocol) failure exhausts its bounded
    # retries within the test budget instead of waiting out 30s leases.
    processor = CommandProcessor(
        persistence, broker, runtime, lease_seconds=0.2, poll_interval=0.02
    )

    async def no_database_connection() -> None:
        pass

    monkeypatch.setattr(sse, "close_idle_db", no_database_connection)

    def client_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_SseBody(
                sse.iter_sse(
                    service,
                    owner_id="owner",
                    conversation_id=conversation_id,
                    last_event_id=int(request.headers.get("Last-Event-ID", "0")),
                )
            ),
        )

    await broker.start()
    try:
        async with AsyncTalkToHarnessesClient("http://tth.test/api/v1/") as client:
            await client._client.aclose()  # pyright: ignore[reportPrivateUsage]
            client._client = httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
                base_url="http://tth.test/api/v1/",
                transport=httpx.MockTransport(client_transport),
            )
            stream = client.stream_conversation_events(conversation_id)
            async with asyncio.timeout(3):
                # Subscribe before delivery so this proves the live wakeup path.
                async for item in stream:
                    if isinstance(item, SyncProjection):
                        break
                await processor.start("startup-test-worker")
                async for item in stream:
                    if isinstance(item, ConversationEvent) and isinstance(
                        item.payload, TurnFailedPayload
                    ):
                        failure = item
                        break
                else:
                    pytest.fail("client did not receive turn_failed")
            await stream.aclose()  # type: ignore[attr-defined]
            assert isinstance(failure.payload, TurnFailedPayload)
            assert failure.payload.message == message
            assert failure.payload.turn_id == submitted.command.target_turn_id

            # A reconnect replays the same durable terminal event.
            replay = client.stream_conversation_events(
                conversation_id, after_sequence=failure.sequence - 1
            )
            assert await asyncio.wait_for(anext(replay), timeout=1) == failure
            await replay.aclose()  # type: ignore[attr-defined]

        stored = persistence.commands[submitted.command.id]
        assert stored.status is CommandStatus.SETTLED
        # Transport/protocol failures are retried a bounded number of times
        # before the turn fails; provider errors fail on the first attempt.
        expected_attempts = _MAX_TRANSIENT_STARTUP_ATTEMPTS if code == "internal_error" else 1
        assert stored.attempts == expected_attempts
        assert stored.lease_expires_at is None
        turns = persistence.turns[conversation_id]
        assert turns[submitted.command.target_turn_id].status is TurnStatus.FAILED
        assert runtime.get_runtime(conversation_id) is None
    finally:
        await processor.stop()
        await runtime.shutdown()
        await broker.stop()
