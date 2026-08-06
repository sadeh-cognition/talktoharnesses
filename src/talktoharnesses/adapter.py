"""Harness protocol — the port of T3's ProviderAdapter.

Drivers satisfy this Protocol structurally (no base class required).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol, runtime_checkable

from talktoharnesses.events import RuntimeEvent
from talktoharnesses.types import (
    ApprovalDecision,
    Capabilities,
    SendTurnInput,
    Session,
    SessionStartInput,
    TurnId,
)


@runtime_checkable
class Harness(Protocol):
    """Unified async interface for every supported coding-agent harness."""

    name: str
    capabilities: Capabilities

    async def start_session(self, input: SessionStartInput | None = None) -> Session:
        """Open a provider session / thread. Must be called before send_turn."""
        ...

    def send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        """Start a turn and yield the event slice for that turn.

        Ends at ``turn.completed`` or ``turn.aborted``. Internally this is a
        filtered view of the session event queue; concurrent consumers of
        ``stream_events()`` still see everything.
        """
        ...

    def stream_events(self) -> AsyncIterator[RuntimeEvent]:
        """Yield all events for the session (all turns), until the session ends."""
        ...

    async def interrupt_turn(self, turn_id: TurnId | str | None = None) -> None:
        """Request interruption of the current (or given) turn."""
        ...

    async def respond(
        self,
        request_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Resolve an open approval request (``request.opened``)."""
        ...

    async def respond_to_user_input(
        self,
        request_id: str,
        answers: Mapping[str, Any],
    ) -> None:
        """Resolve a ``user-input.requested`` prompt."""
        ...

    async def stop_session(self) -> None:
        """End the current session without necessarily tearing down the process."""
        ...

    async def aclose(self) -> None:
        """Release all resources (process, sockets, client). Safe to call twice."""
        ...
