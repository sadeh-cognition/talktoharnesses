"""HTTP request/response bodies and SSE frames of the split service API.

Every harness split service exposes the identical `/v1/` surface; these models
are the only wire contract between the proxy's remote adapter and a split.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from tth_types.adapter import HarnessSession
from tth_types.base import FROZEN
from tth_types.enums import HarnessKind
from tth_types.events import EventPayload, InteractionRequestedPayload
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    LaunchSnapshot,
    VersionAdvisory,
)
from tth_types.process import ProcessEvent

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class SplitHealth(BaseModel):
    model_config = FROZEN

    status: Literal["ok"] = "ok"
    kind: HarnessKind
    split_version: str
    tth_types_version: str
    sessions: int = 0


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class ProbeRequest(BaseModel):
    model_config = FROZEN

    configuration: HarnessConfiguration
    adapter_version: str
    redaction_patterns: tuple[str, ...] = ()


class ProbeResponse(BaseModel):
    model_config = FROZEN

    capabilities: HarnessCapabilities
    launch: LaunchSnapshot
    # Computed against the split's packaged compatibility floor.
    advisory: VersionAdvisory | None = None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    model_config = FROZEN

    # Chosen by the proxy so cleanup can address a session even when the
    # create response is lost or the caller is cancelled.
    session_id: UUID = Field(default_factory=uuid4)
    mode: Literal["start", "resume"]
    conversation_id: UUID
    binding_id: UUID
    configuration: HarnessConfiguration
    # Required when mode == "resume".
    native_session_id: str | None = None
    adapter_version: str
    redaction_patterns: tuple[str, ...] = ()
    # Native dedupe state imported into the fresh adapter before streaming.
    seen_native_ids: tuple[str, ...] = ()
    seen_stream_offsets: tuple[str, ...] = ()


class SessionCreated(BaseModel):
    model_config = FROZEN

    session_id: UUID
    session: HarnessSession
    launch: LaunchSnapshot
    # None for SDK-managed harnesses that expose no supervised process.
    pid: int | None = None


class SteerResult(BaseModel):
    model_config = FROZEN

    accepted: bool


class TerminateRequest(BaseModel):
    model_config = FROZEN

    reason: str | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SplitError(BaseModel):
    """Serialized DomainError; ``code`` round-trips via tth_types.enums.ErrorCode."""

    model_config = FROZEN

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SSE frames (GET /v1/sessions/{sid}/events)
#
# Frame `event:` names are the FRAME_* constants; `data:` is the JSON dump of
# the matching model. `id:` is a per-session monotonic counter with no replay
# semantics — the proxy is the sole client and reconnection means the harness
# stream ended.
# ---------------------------------------------------------------------------

FRAME_HARNESS_EVENT = "harness_event"
FRAME_INTERACTION = "interaction"
FRAME_PROCESS = "process"
FRAME_END = "end"


class HarnessEventFrame(BaseModel):
    model_config = FROZEN

    item: EventPayload
    # Per-event native dedupe deltas, mirrored by the proxy's remote adapter.
    new_native_ids: tuple[str, ...] = ()
    new_stream_offsets: tuple[str, ...] = ()


class InteractionFrame(BaseModel):
    model_config = FROZEN

    payload: InteractionRequestedPayload
    provider_correlation: dict[str, str] = Field(default_factory=dict)
    new_native_ids: tuple[str, ...] = ()
    new_stream_offsets: tuple[str, ...] = ()


class ProcessSnapshot(BaseModel):
    """Current supervised-process observation accompanying each process frame."""

    model_config = FROZEN

    pid: int | None = None
    returncode: int | None = None
    redacted_stderr_tail: str = ""
    forced: bool = False
    forced_reason: str | None = None
    stderr_truncated: bool = False
    retained_stderr_bytes: int = 0


class ProcessFrame(BaseModel):
    model_config = FROZEN

    event: ProcessEvent
    snapshot: ProcessSnapshot


class EndFrame(BaseModel):
    model_config = FROZEN

    reason: Literal["closed", "stream_ended", "terminated"]
