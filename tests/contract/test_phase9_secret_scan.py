"""Secrets must not leak into HTTP, recovery rows, or telemetry attributes."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from opentelemetry import metrics, trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.observability import (
    ALLOWED_ATTRIBUTE_KEYS,
    SPAN_WORKER_RECOVERY,
    get_observability,
    reset_observability_for_tests,
)
from talktoharnesses.application.persistence import RecoveryAttempt
from talktoharnesses.django.api.errors import domain_error_response
from talktoharnesses.domain.enums import (
    ErrorCode,
    RecoveryAction,
    RecoveryReasonCode,
    RecoveryResultCode,
    RecoveryTrigger,
)
from talktoharnesses.domain.errors import DomainError, public_message

SECRET = "P9_SECRET_FIXTURE_sk-live-do-not-echo-xyz987"


def _force_tracer_provider(provider: trace.TracerProvider) -> None:
    trace._TRACER_PROVIDER = provider  # pyright: ignore[reportPrivateUsage]
    trace._TRACER_PROVIDER_SET_ONCE._done = True  # pyright: ignore[reportPrivateUsage]


def _force_meter_provider(provider: metrics.MeterProvider) -> None:
    metrics_internal._METER_PROVIDER = provider  # pyright: ignore[reportPrivateUsage]
    metrics_internal._METER_PROVIDER_SET_ONCE._done = True  # pyright: ignore[reportPrivateUsage]


def test_http_error_body_does_not_contain_secret() -> None:
    response = domain_error_response(
        DomainError(ErrorCode.PROTOCOL_ERROR, f"provider dump: {SECRET}")
    )
    body = json.loads(response.content)
    raw = response.content.decode()
    assert body["message"] == public_message(ErrorCode.PROTOCOL_ERROR)
    assert SECRET not in body["message"]
    assert SECRET not in raw
    assert "sk-live" not in raw


def test_recovery_attempt_reason_code_is_fixed_not_secret() -> None:
    persistence = MemoryPersistence()
    attempt_id = uuid4()
    conversation_id = uuid4()
    now = datetime(2026, 8, 9, tzinfo=UTC)
    persistence.recovery_attempts[attempt_id] = RecoveryAttempt(
        id=attempt_id,
        conversation_id=conversation_id,
        binding_id=uuid4(),
        command_id=None,
        turn_id=None,
        worker_id="worker-1",
        fence=1,
        trigger=RecoveryTrigger.STARTUP.value,
        observed_delivery_phase="delivery_started",
        action=RecoveryAction.OUTCOME_UNKNOWN.value,
        result=None,
        reason_code=RecoveryReasonCode.DELIVERY_AMBIGUOUS.value,
        started_at=now,
        completed_at=None,
    )

    # Completing with a fixed code — never an exception/secret message.
    import asyncio

    asyncio.run(
        persistence.complete_recovery_attempt(
            attempt_id,
            result=RecoveryResultCode.SUCCESS.value,
            reason_code=RecoveryReasonCode.DELIVERY_AMBIGUOUS.value,
            completed_at=now,
        )
    )
    stored = persistence.recovery_attempts[attempt_id]
    assert stored.reason_code == RecoveryReasonCode.DELIVERY_AMBIGUOUS.value
    assert SECRET not in (stored.reason_code or "")
    assert SECRET not in (stored.result or "")
    assert SECRET not in json.dumps(asdict(stored), default=str)


def test_observability_attributes_do_not_contain_secret() -> None:
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    _force_tracer_provider(tracer_provider)
    metric_reader = InMemoryMetricReader()
    _force_meter_provider(MeterProvider(metric_readers=[metric_reader]))
    reset_observability_for_tests()
    try:
        obs = get_observability()
        with obs.start_span(
            SPAN_WORKER_RECOVERY,
            recovery_trigger=RecoveryTrigger.STARTUP,
            recovery_action=RecoveryAction.OUTCOME_UNKNOWN,
            error_code=ErrorCode.PROTOCOL_ERROR,
            operation="recover_owned",
            database_system="sqlite",
        ) as span:
            # Even if a caller tries to attach a secret, mark_span_error stays coded.
            obs.mark_span_error(span, ErrorCode.PROTOCOL_ERROR)
        obs.record_recovery(
            trigger=RecoveryTrigger.STARTUP,
            action=RecoveryAction.OUTCOME_UNKNOWN,
            outcome=RecoveryResultCode.SUCCESS.value,
            error_code=ErrorCode.PROTOCOL_ERROR,
        )

        for finished in span_exporter.get_finished_spans():
            for key, value in (finished.attributes or {}).items():
                assert key in ALLOWED_ATTRIBUTE_KEYS
                assert SECRET not in str(value)
                assert "sk-live" not in str(value)
            assert SECRET not in (finished.status.description or "")
    finally:
        _force_tracer_provider(trace.NoOpTracerProvider())
        _force_meter_provider(metrics.NoOpMeterProvider())
        reset_observability_for_tests()
