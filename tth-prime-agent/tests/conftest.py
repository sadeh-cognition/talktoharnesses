"""Shared fixtures: fake adapter injection and a clean session store per test."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from tth_types.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from tth_types.enums import HarnessKind, InteractionKind
from tth_types.events import (
    AssistantMessageCompletedPayload,
    AssistantMessageStartedPayload,
    HarnessEvent,
    InteractionRequestedPayload,
    TurnCompletedPayload,
)
from tth_types.harness import (
    ApprovalRequestPayload,
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
)

from tth_prime_agent import service, sessions

# Default-on OTel export must never engage in tests: opt out before any
# test can import tth_prime_agent.asgi and start exporters.
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "0")


class FakeAdapter:
    """Protocol-complete in-memory adapter emitting a scripted turn."""

    kind = HarnessKind.PRIME_AGENT
    sdk_managed = True

    def __init__(self) -> None:
        self._queue: asyncio.Queue[HarnessEvent | HarnessInteractionRequest | None] = (
            asyncio.Queue()
        )
        self._seen: set[str] = set()
        self._imported: tuple[frozenset[str], frozenset[str]] | None = None
        self.redaction_patterns: tuple[str, ...] = ()
        self.preflight_modes: list[str] = []
        self.answers: list[InteractionAnswer] = []
        self.interrupted = False
        self.closed = False
        self.session: HarnessSession | None = None

    # hooks
    def set_redaction_patterns(self, patterns: tuple[str, ...]) -> None:
        self.redaction_patterns = patterns

    def import_seen(self, native_ids: frozenset[str], offsets: frozenset[str]) -> None:
        self._imported = (native_ids, offsets)
        self._seen.update(native_ids)

    def export_seen(self) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(self._seen), frozenset()

    def preflight_operation(self, mode: str) -> None:
        self.preflight_modes.append(mode)

    # protocol
    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        return HarnessCapabilities(
            kind=HarnessKind.PRIME_AGENT,
            version="1.18.19",
            supports_resume=True,
            supports_interrupt=True,
        )

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        self.session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.PRIME_AGENT,
            native_session_id=str(uuid4()),
            model=request.configuration.model,
        )
        return self.session

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        self.session = HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=HarnessKind.PRIME_AGENT,
            native_session_id=request.native_session_id,
        )
        return self.session

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        native_id = f"native-{request.turn_id}"
        self._seen.add(native_id)
        message_id = uuid4()
        await self._queue.put(
            AssistantMessageStartedPayload(turn_id=request.turn_id, message_id=message_id)
        )
        await self._queue.put(
            AssistantMessageCompletedPayload(
                turn_id=request.turn_id,
                message_id=message_id,
                text=f"echo: {request.prompt}",
            )
        )
        await self._queue.put(TurnCompletedPayload(turn_id=request.turn_id))

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        return True

    async def interrupt(self, session: HarnessSession) -> None:
        self.interrupted = True

    async def answer_interaction(
        self, session: HarnessSession, answer: InteractionAnswer
    ) -> None:
        self.answers.append(answer)

    def events(
        self, session: HarnessSession
    ) -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
        async def _gen() -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        self.closed = True
        await self._queue.put(None)

    # test helpers
    async def emit_interaction(self, turn_id: Any) -> None:
        await self._queue.put(
            HarnessInteractionRequest(
                payload=InteractionRequestedPayload(
                    turn_id=turn_id,
                    interaction_id=uuid4(),
                    kind=InteractionKind.APPROVAL,
                    request=ApprovalRequestPayload(tool_name="bash"),
                ),
                provider_correlation={"tool_name": "bash"},
            )
        )


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAdapter]:
    adapter = FakeAdapter()
    monkeypatch.setattr(service, "adapter_factory", lambda: adapter)
    sessions.reset_session_store_for_tests()
    yield adapter
    sessions.reset_session_store_for_tests()
