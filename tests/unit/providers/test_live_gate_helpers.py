"""Fail-closed behavior for shared live-gate event collection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from tests.live.helpers import collect_turn

from talktoharnesses.domain.enums import HarnessKind, InteractionKind
from talktoharnesses.domain.events import (
    InteractionRequestedPayload,
    TurnCompletedPayload,
    TurnFailedPayload,
)
from talktoharnesses.domain.models import ApprovalRequestPayload, InteractionAnswer
from talktoharnesses.providers.adapter import HarnessSession


def _session() -> HarnessSession:
    return HarnessSession(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        kind=HarnessKind.GROK,
    )


class _InteractiveAdapter:
    def __init__(self) -> None:
        self.turn_id = uuid4()
        self.interaction_id = uuid4()
        self.answer: InteractionAnswer | None = None

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None:
        del session
        self.answer = answer

    def events(self, session: HarnessSession) -> AsyncIterator[object]:
        del session

        async def _events() -> AsyncIterator[object]:
            yield InteractionRequestedPayload(
                turn_id=self.turn_id,
                interaction_id=self.interaction_id,
                kind=InteractionKind.APPROVAL,
                request=ApprovalRequestPayload(),
            )
            assert self.answer is not None
            yield TurnCompletedPayload(turn_id=self.turn_id, terminal_reason="end_turn")

        return _events()


@pytest.mark.asyncio
async def test_collect_turn_answers_interaction_before_waiting_for_terminal() -> None:
    adapter = _InteractiveAdapter()
    events = await collect_turn(adapter, _session(), timeout=1.0)
    assert [getattr(event, "type", None) for event in events] == [
        "interaction_requested",
        "turn_completed",
    ]
    assert adapter.answer is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "completed_without_interaction"])
async def test_collect_turn_rejects_invalid_live_evidence(terminal: str) -> None:
    turn_id = uuid4()

    class _Adapter:
        def events(self, session: HarnessSession) -> AsyncIterator[object]:
            del session

            async def _events() -> AsyncIterator[object]:
                if terminal == "failed":
                    yield TurnFailedPayload(
                        turn_id=turn_id,
                        error_code="provider_error",
                        message="failed",
                    )
                else:
                    yield TurnCompletedPayload(turn_id=turn_id, terminal_reason="end_turn")

            return _events()

        async def answer_interaction(
            self,
            session: HarnessSession,
            answer: InteractionAnswer,
        ) -> None:
            del session, answer

    with pytest.raises(AssertionError):
        await collect_turn(_Adapter(), _session(), timeout=1.0)
