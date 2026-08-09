"""OpenTelemetry library instrumentation (Phase 9 WP4)."""

from __future__ import annotations

from opentelemetry import metrics, trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from talktoharnesses.application.observability import (
    ALLOWED_ATTRIBUTE_KEYS,
    ATTR_COMMAND_KIND,
    ATTR_OUTCOME,
    ATTR_RECOVERY_TRIGGER,
    SPAN_COMMAND_DELIVERY,
    SPAN_WORKER_RECOVERY,
    get_observability,
    reset_observability_for_tests,
)
from talktoharnesses.domain.enums import CommandKind, RecoveryAction, RecoveryTrigger


def _force_tracer_provider(provider: trace.TracerProvider) -> None:
    # OpenTelemetry allows only one global provider set; tests override via internals.
    trace._TRACER_PROVIDER = provider  # pyright: ignore[reportPrivateUsage]
    trace._TRACER_PROVIDER_SET_ONCE._done = True  # pyright: ignore[reportPrivateUsage]


def _force_meter_provider(provider: metrics.MeterProvider) -> None:
    metrics_internal._METER_PROVIDER = provider  # pyright: ignore[reportPrivateUsage]
    metrics_internal._METER_PROVIDER_SET_ONCE._done = True  # pyright: ignore[reportPrivateUsage]


def _install_sdk() -> InMemorySpanExporter:
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    _force_tracer_provider(tracer_provider)

    metric_reader = InMemoryMetricReader()
    _force_meter_provider(MeterProvider(metric_readers=[metric_reader]))
    reset_observability_for_tests()
    return span_exporter


def _reset_providers() -> None:
    _force_tracer_provider(trace.NoOpTracerProvider())
    _force_meter_provider(metrics.NoOpMeterProvider())
    reset_observability_for_tests()


def test_spans_and_attributes_are_allowlisted() -> None:
    span_exporter = _install_sdk()
    try:
        obs = get_observability()
        with obs.start_span(
            SPAN_WORKER_RECOVERY,
            recovery_trigger=RecoveryTrigger.STARTUP,
            recovery_action=RecoveryAction.NO_ACTION,
            database_system="sqlite",
            operation="recover_owned",
        ):
            pass
        with obs.start_span(
            SPAN_COMMAND_DELIVERY,
            command_kind=CommandKind.SUBMIT_TURN,
            outcome="delivered",
        ):
            pass
        obs.record_command(kind=CommandKind.SUBMIT_TURN, outcome="delivered")
        obs.record_recovery(
            trigger=RecoveryTrigger.STARTUP,
            action=RecoveryAction.NO_ACTION,
            outcome="no_action",
        )

        spans = span_exporter.get_finished_spans()
        assert {span.name for span in spans} == {SPAN_WORKER_RECOVERY, SPAN_COMMAND_DELIVERY}
        for span in spans:
            keys = frozenset(span.attributes or {})
            assert keys <= ALLOWED_ATTRIBUTE_KEYS
        recovery = next(s for s in spans if s.name == SPAN_WORKER_RECOVERY)
        assert recovery.attributes is not None
        assert recovery.attributes[ATTR_RECOVERY_TRIGGER] == RecoveryTrigger.STARTUP.value
        command = next(s for s in spans if s.name == SPAN_COMMAND_DELIVERY)
        assert command.attributes is not None
        assert command.attributes[ATTR_COMMAND_KIND] == CommandKind.SUBMIT_TURN.value
        assert command.attributes[ATTR_OUTCOME] == "delivered"
    finally:
        _reset_providers()


def test_noop_without_sdk_does_not_raise() -> None:
    _reset_providers()
    obs = get_observability()
    with obs.start_span(
        SPAN_WORKER_RECOVERY,
        recovery_trigger=RecoveryTrigger.STARTUP,
        operation="recover_owned",
    ) as span:
        obs.mark_span_error(span, "invalid_state")
    obs.record_command(kind=CommandKind.INTERRUPT, outcome="delivered")
    obs.record_recovery(
        trigger=RecoveryTrigger.TAKEOVER,
        action=RecoveryAction.OUTCOME_UNKNOWN,
        outcome="success",
    )
    obs.set_gauge_sample("tth.worker_ready", True)
    obs.record_startup_recovery_duration(0.01, database_system="sqlite")
