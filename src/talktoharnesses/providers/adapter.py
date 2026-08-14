"""Fixed harness adapter contract — provider-specific types must not leak."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from talktoharnesses.domain._base import FROZEN
from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.events import HarnessEvent, InteractionRequestedPayload
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    InteractionAnswer,
    LaunchSnapshot,
)


class StartSessionRequest(BaseModel):
    model_config = FROZEN

    conversation_id: UUID
    binding_id: UUID
    configuration: HarnessConfiguration
    launch: LaunchSnapshot


class ResumeSessionRequest(BaseModel):
    model_config = FROZEN

    conversation_id: UUID
    binding_id: UUID
    configuration: HarnessConfiguration
    native_session_id: str
    launch: LaunchSnapshot


class TurnRequest(BaseModel):
    model_config = FROZEN

    turn_id: UUID
    command_id: UUID | None = None
    prompt: str
    model: str | None = None


class SteerRequest(BaseModel):
    model_config = FROZEN

    turn_id: UUID
    command_id: UUID | None = None
    prompt: str


class HarnessSession(BaseModel):
    """Opaque session handle with no live process/SDK objects."""

    model_config = FROZEN

    conversation_id: UUID
    binding_id: UUID
    kind: HarnessKind
    native_session_id: str | None = None
    model: str | None = None
    mode: str | None = None
    effort: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class HarnessInteractionRequest(BaseModel):
    """Private adapter envelope for canonical requests and native correlation."""

    model_config = FROZEN

    payload: InteractionRequestedPayload
    provider_correlation: dict[str, str] = Field(default_factory=dict)


class HarnessAdapter(Protocol):
    """Asynchronous provider adapter. Methods are fixed for all harnesses."""

    kind: HarnessKind

    async def probe(self, config: HarnessConfiguration) -> HarnessCapabilities: ...

    async def start(self, request: StartSessionRequest) -> HarnessSession: ...

    async def resume(self, request: ResumeSessionRequest) -> HarnessSession: ...

    async def submit(self, session: HarnessSession, request: TurnRequest) -> None: ...

    async def steer(self, session: HarnessSession, request: SteerRequest) -> bool: ...

    async def interrupt(self, session: HarnessSession) -> None: ...

    async def answer_interaction(
        self,
        session: HarnessSession,
        answer: InteractionAnswer,
    ) -> None: ...

    def events(
        self,
        session: HarnessSession,
    ) -> AsyncIterator[HarnessEvent | HarnessInteractionRequest]:
        """Stream normalized harness events until the session ends."""
        ...

    async def close(self, session: HarnessSession) -> None: ...
