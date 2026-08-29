"""Wire-contract smoke tests for the shared schema package."""

from __future__ import annotations

from uuid import uuid4

from tth_types.enums import ErrorCode, HarnessKind
from tth_types.errors import DomainError, public_message
from tth_types.events import UsageUpdatedPayload, event_payload_adapter
from tth_types.harness import HarnessConfiguration
from tth_types.split_api import (
    CreateSessionRequest,
    HarnessEventFrame,
    ProbeRequest,
    SplitError,
)


def _config() -> HarnessConfiguration:
    return HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp")


def test_event_payload_round_trip() -> None:
    payload = UsageUpdatedPayload(turn_id=uuid4(), input_tokens=5, output_tokens=7)
    frame = HarnessEventFrame(item=payload, new_native_ids=("n1",))
    decoded = HarnessEventFrame.model_validate_json(frame.model_dump_json())
    assert decoded == frame
    assert event_payload_adapter.validate_json(payload.model_dump_json()) == payload


def test_probe_and_session_bodies_round_trip() -> None:
    probe = ProbeRequest(configuration=_config(), adapter_version="1")
    assert ProbeRequest.model_validate_json(probe.model_dump_json()) == probe
    create = CreateSessionRequest(
        mode="start",
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=_config(),
        adapter_version="1",
    )
    assert CreateSessionRequest.model_validate_json(create.model_dump_json()) == create


def test_error_codes_round_trip() -> None:
    error = DomainError(ErrorCode.NOT_FOUND, "missing", details={"kind": "grok"})
    wire = SplitError(
        code=error.code.value,
        message=public_message(error.code, details=error.details),
        details=error.details,
    )
    decoded = SplitError.model_validate_json(wire.model_dump_json())
    assert ErrorCode(decoded.code) is ErrorCode.NOT_FOUND
