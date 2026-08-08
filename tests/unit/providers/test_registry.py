"""Adapter registry isolation and registration rules."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from talktoharnesses.domain import (
    DomainError,
    ErrorCode,
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessKind,
    InteractionAnswer,
)
from talktoharnesses.domain.events import HarnessEvent, TurnStartedPayload
from talktoharnesses.providers import (
    AdapterRegistry,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)


class _FakeAdapter:
    def __init__(self, kind: HarnessKind) -> None:
        self.kind = kind
        self.id = uuid4()

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities:
        return HarnessCapabilities(kind=self.kind, version="0")

    async def start(self, request: StartSessionRequest) -> HarnessSession:
        return HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=self.kind,
        )

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession:
        return HarnessSession(
            conversation_id=request.conversation_id,
            binding_id=request.binding_id,
            kind=self.kind,
            native_session_id=request.native_session_id,
        )

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None:
        return None

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool:
        return False

    async def interrupt(self, session: HarnessSession) -> None:
        return None

    async def answer_interaction(self, session: HarnessSession, answer: InteractionAnswer) -> None:
        return None

    def events(self, session: HarnessSession) -> AsyncIterator[HarnessEvent]:
        async def _gen() -> AsyncIterator[HarnessEvent]:
            if False:  # pragma: no cover — keep async-generator shape
                yield TurnStartedPayload(turn_id=uuid4())
            return

        return _gen()

    async def close(self, session: HarnessSession) -> None:
        return None


def test_register_and_create() -> None:
    reg = AdapterRegistry()
    reg.register(HarnessKind.GROK, lambda: _FakeAdapter(HarnessKind.GROK))
    a = reg.create(HarnessKind.GROK)
    b = reg.create(HarnessKind.GROK)
    assert a is not b
    assert a.kind is HarnessKind.GROK
    assert HarnessKind.GROK in reg
    assert reg.kinds() == frozenset({HarnessKind.GROK})


def test_duplicate_registration_rejected() -> None:
    reg = AdapterRegistry()
    reg.register(HarnessKind.GROK, lambda: _FakeAdapter(HarnessKind.GROK))
    with pytest.raises(DomainError) as ei:
        reg.register(HarnessKind.GROK, lambda: _FakeAdapter(HarnessKind.GROK))
    assert ei.value.code is ErrorCode.DUPLICATE_REGISTRATION


def test_missing_registration() -> None:
    reg = AdapterRegistry()
    with pytest.raises(DomainError) as ei:
        reg.create(HarnessKind.CODEX)
    assert ei.value.code is ErrorCode.HARNESS_NOT_REGISTERED


def test_registry_isolation() -> None:
    a = AdapterRegistry()
    b = AdapterRegistry()
    a.register(HarnessKind.GROK, lambda: _FakeAdapter(HarnessKind.GROK))
    assert HarnessKind.GROK in a
    assert HarnessKind.GROK not in b
