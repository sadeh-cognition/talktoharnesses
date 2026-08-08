"""Process-local lifecycle events (not conversation canonical envelopes)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter

from talktoharnesses.domain._base import FROZEN


class ProcessStartedEvent(BaseModel):
    model_config = FROZEN

    type: Literal["started"] = "started"
    process_id: UUID
    pid: int


class ProcessStderrTruncatedEvent(BaseModel):
    model_config = FROZEN

    type: Literal["stderr_truncated"] = "stderr_truncated"
    process_id: UUID
    retained_bytes: int


class ProcessSilenceWarningEvent(BaseModel):
    model_config = FROZEN

    type: Literal["silence_warning"] = "silence_warning"
    process_id: UUID


class ProcessExitedEvent(BaseModel):
    model_config = FROZEN

    type: Literal["exited"] = "exited"
    process_id: UUID
    exit_code: int | None = None


class ProcessForcedTerminationEvent(BaseModel):
    model_config = FROZEN

    type: Literal["forced_termination"] = "forced_termination"
    process_id: UUID
    reason: str | None = None


ProcessEvent = Annotated[
    ProcessStartedEvent
    | ProcessStderrTruncatedEvent
    | ProcessSilenceWarningEvent
    | ProcessExitedEvent
    | ProcessForcedTerminationEvent,
    Field(discriminator="type"),
]

process_event_adapter: TypeAdapter[ProcessEvent] = TypeAdapter(ProcessEvent)
