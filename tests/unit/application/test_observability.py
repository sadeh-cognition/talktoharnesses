"""OpenTelemetry library instrumentation (Phase 9 WP4)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from opentelemetry import metrics, trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from talktoharnesses.application.observability import (
    ALLOWED_ATTRIBUTE_KEYS,
    ATTR_COMMAND_KIND,
    ATTR_OUTCOME,
    ATTR_RECOVERY_TRIGGER,
    HIST_TOKEN_COST,
    HIST_TURN_TOKENS,
    SPAN_COMMAND_DELIVERY,
    SPAN_WORKER_RECOVERY,
    get_observability,
    reset_observability_for_tests,
)
from talktoharnesses.domain.enums import CommandKind, RecoveryAction, RecoveryTrigger
from talktoharnesses.domain.events import (
    ConversationEvent,
    CostUpdatedPayload,
    TurnCompletedPayload,
    UsageUpdatedPayload,
)


def _force_tracer_provider(provider: trace.TracerProvider) -> None:
    # OpenTelemetry allows only one global provider set; tests override via internals.
    trace._TRACER_PROVIDER = provider  # pyright: ignore[reportPrivateUsage]
    trace._TRACER_PROVIDER_SET_ONCE._done = True  # pyright: ignore[reportPrivateUsage]


def _force_meter_provider(provider: metrics.MeterProvider) -> None:
    metrics_internal._METER_PROVIDER = provider  # pyright: ignore[reportPrivateUsage]
    metrics_internal._METER_PROVIDER_SET_ONCE._done = True  # pyright: ignore[reportPrivateUsage]


def _install_sdk_with_metrics() -> tuple[InMemorySpanExporter, InMemoryMetricReader]:
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    _force_tracer_provider(tracer_provider)

    metric_reader = InMemoryMetricReader()
    _force_meter_provider(MeterProvider(metric_readers=[metric_reader]))
    reset_observability_for_tests()
    return span_exporter, metric_reader


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


def _recorded(reader: InMemoryMetricReader, name: str) -> tuple[int, float]:
    """How many values the named histogram holds, and what they add up to.

    The instruments carry no attributes, so every recording lands in one data
    point; the count is what separates one sample per turn from several.
    """
    data = reader.get_metrics_data()
    count = 0
    total = 0.0
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name:
                    continue
                histogram = metric.data
                assert isinstance(histogram, Histogram)
                for point in histogram.data_points:
                    count += point.count
                    total += float(point.sum)
    return count, total


def _event(
    conversation_id: UUID,
    sequence: int,
    payload: CostUpdatedPayload | TurnCompletedPayload | UsageUpdatedPayload,
) -> ConversationEvent:
    return ConversationEvent(
        conversation_id=conversation_id,
        sequence=sequence,
        timestamp=datetime(2026, 9, 10, tzinfo=UTC),
        type=payload.type,
        payload=payload,
    )


def test_a_turn_records_its_tokens_once_however_often_it_reports() -> None:
    """Usage payloads supersede one another, so only the last one is the turn's.

    Recording each report would count the same tokens once per report, which is
    exactly what a turn that reports while it runs does many times over.
    """
    _, reader = _install_sdk_with_metrics()
    try:
        obs = get_observability()
        conversation, turn = uuid4(), uuid4()
        obs.observe_committed_events(
            [
                _event(conversation, 1, UsageUpdatedPayload(turn_id=turn, total_tokens=1_000)),
                _event(conversation, 2, UsageUpdatedPayload(turn_id=turn, total_tokens=2_500)),
            ]
        )
        assert _recorded(reader, HIST_TURN_TOKENS) == (0, 0.0)

        obs.observe_committed_events(
            [
                _event(conversation, 3, UsageUpdatedPayload(turn_id=turn, total_tokens=4_000)),
                _event(conversation, 4, TurnCompletedPayload(turn_id=turn)),
            ]
        )
        assert _recorded(reader, HIST_TURN_TOKENS) == (1, 4_000)
    finally:
        _reset_providers()


def test_a_turn_reporting_again_after_many_others_is_not_counted_twice() -> None:
    """A busy process is where double-counting would show up, so hold no LRU."""
    _, reader = _install_sdk_with_metrics()
    try:
        obs = get_observability()
        conversation, turn = uuid4(), uuid4()
        obs.observe_committed_events(
            [_event(conversation, 1, UsageUpdatedPayload(turn_id=turn, total_tokens=1_000))]
        )
        for index in range(2_000):
            other = uuid4()
            obs.observe_committed_events(
                [_event(uuid4(), index + 2, UsageUpdatedPayload(turn_id=other, total_tokens=7))]
            )
        obs.observe_committed_events(
            [
                _event(conversation, 3_000, UsageUpdatedPayload(turn_id=turn, total_tokens=1_500)),
                _event(conversation, 3_001, TurnCompletedPayload(turn_id=turn)),
            ]
        )
        # The evicted turn contributes its final total once, never that total
        # on top of the 1 000 it reported before it was evicted.
        assert _recorded(reader, HIST_TURN_TOKENS) == (1, 1_500)
    finally:
        _reset_providers()


def test_a_turn_without_a_provider_total_still_reaches_the_metric() -> None:
    """Several providers report no total; adapters must not invent one.

    A metric that counted only the providers that send a total would undercount
    the rest, so the sink adds the halves it was given.
    """
    _, reader = _install_sdk_with_metrics()
    try:
        obs = get_observability()
        conversation, turn = uuid4(), uuid4()
        obs.observe_committed_events(
            [
                _event(
                    conversation,
                    1,
                    UsageUpdatedPayload(turn_id=turn, input_tokens=30, output_tokens=8),
                ),
                _event(conversation, 2, TurnCompletedPayload(turn_id=turn)),
            ]
        )
        assert _recorded(reader, HIST_TURN_TOKENS) == (1, 38)
    finally:
        _reset_providers()


def test_token_cost_histogram_carries_cost_alone() -> None:
    """Tokens and USD in one distribution make any percentile over it useless."""
    _, reader = _install_sdk_with_metrics()
    try:
        obs = get_observability()
        conversation, turn = uuid4(), uuid4()
        obs.observe_committed_events(
            [
                _event(conversation, 1, UsageUpdatedPayload(turn_id=turn, total_tokens=4_000)),
                _event(conversation, 2, CostUpdatedPayload(turn_id=turn, cost="0.25")),
                _event(conversation, 3, TurnCompletedPayload(turn_id=turn)),
            ]
        )
        assert _recorded(reader, HIST_TOKEN_COST) == (1, 0.25)
        assert _recorded(reader, HIST_TURN_TOKENS) == (1, 4_000)
    finally:
        _reset_providers()
