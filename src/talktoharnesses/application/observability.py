"""Library-only OpenTelemetry instrumentation (Phase 9 WP4).

Owns the instrumentation scope, fixed span/metric names, attribute allowlist,
and typed recording helpers. Callers pass enums or fixed strings only — never
arbitrary attribute dictionaries, exception objects, or payload-bearing span
events. With no host SDK configured, the OpenTelemetry API is a no-op.
"""

from __future__ import annotations

import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import Attributes

from talktoharnesses.domain.enums import (
    CommandKind,
    ErrorCode,
    HarnessKind,
    InteractionKind,
    ProcessStatus,
    RecoveryAction,
    RecoveryTrigger,
    ToolOutcome,
)
from talktoharnesses.domain.events import (
    CostUpdatedPayload,
    InteractionRequestedPayload,
    InteractionResolvedPayload,
    ProcessExitedPayload,
    ProcessForcedTerminationPayload,
    ProcessStderrTruncatedPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    UsageUpdatedPayload,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from talktoharnesses.domain.events import ConversationEvent
    from talktoharnesses.domain.transitions import ConversationState

INSTRUMENTATION_SCOPE: Final = "talktoharnesses"

# --- Span names -------------------------------------------------------------

SPAN_WORKER_RECOVERY: Final = "worker.recovery"
SPAN_COMMAND_DELIVERY: Final = "command.delivery"
SPAN_RUNTIME_START: Final = "runtime.start"
SPAN_RUNTIME_RESUME: Final = "runtime.resume"
SPAN_HARNESS_PROBE: Final = "harness.probe"
SPAN_TURN_OBSERVATION: Final = "turn.observation"
SPAN_INTERACTION_RESOLUTION: Final = "interaction.resolution"
SPAN_SSE_REPLAY: Final = "sse.replay"
SPAN_SHUTDOWN: Final = "shutdown"

SPAN_NAMES: Final[frozenset[str]] = frozenset(
    {
        SPAN_WORKER_RECOVERY,
        SPAN_COMMAND_DELIVERY,
        SPAN_RUNTIME_START,
        SPAN_RUNTIME_RESUME,
        SPAN_HARNESS_PROBE,
        SPAN_TURN_OBSERVATION,
        SPAN_INTERACTION_RESOLUTION,
        SPAN_SSE_REPLAY,
        SPAN_SHUTDOWN,
    }
)

# --- Attribute allowlist ----------------------------------------------------

ATTR_HARNESS_KIND: Final = "tth.harness.kind"
ATTR_COMMAND_KIND: Final = "tth.command.kind"
ATTR_OPERATION: Final = "tth.operation"
ATTR_OUTCOME: Final = "tth.outcome"
ATTR_ERROR_CODE: Final = "tth.error.code"
ATTR_RECOVERY_TRIGGER: Final = "tth.recovery.trigger"
ATTR_RECOVERY_ACTION: Final = "tth.recovery.action"
ATTR_PROCESS_STATUS: Final = "tth.process.status"
ATTR_INTERACTION_KIND: Final = "tth.interaction.kind"
ATTR_TOOL_OUTCOME: Final = "tth.tool.outcome"
ATTR_TRANSPORT: Final = "tth.transport"
ATTR_DATABASE_SYSTEM: Final = "tth.database.system"

ALLOWED_ATTRIBUTE_KEYS: Final[frozenset[str]] = frozenset(
    {
        ATTR_HARNESS_KIND,
        ATTR_COMMAND_KIND,
        ATTR_OPERATION,
        ATTR_OUTCOME,
        ATTR_ERROR_CODE,
        ATTR_RECOVERY_TRIGGER,
        ATTR_RECOVERY_ACTION,
        ATTR_PROCESS_STATUS,
        ATTR_INTERACTION_KIND,
        ATTR_TOOL_OUTCOME,
        ATTR_TRANSPORT,
        ATTR_DATABASE_SYSTEM,
    }
)

# --- Metric names -----------------------------------------------------------

METRIC_COMMANDS: Final = "tth.commands"
METRIC_RECOVERY_ATTEMPTS: Final = "tth.recovery_attempts"
METRIC_PROCESS_EXITS: Final = "tth.process_exits"
METRIC_STDERR_TRUNCATIONS: Final = "tth.stderr_truncations"
METRIC_TOOL_TERMINAL_OUTCOMES: Final = "tth.tool_terminal_outcomes"
METRIC_INTERACTION_REQUESTS: Final = "tth.interaction_requests"
METRIC_INTERACTION_RESOLUTIONS: Final = "tth.interaction_resolutions"
METRIC_SSE_RECONNECTS: Final = "tth.sse_reconnects"
METRIC_USAGE_OBSERVATIONS: Final = "tth.usage_observations"
METRIC_COST_OBSERVATIONS: Final = "tth.cost_observations"

GAUGE_ACCEPTED_QUEUE_DEPTH: Final = "tth.accepted_queue_depth"
GAUGE_OWNED_CONVERSATIONS: Final = "tth.owned_conversations"
GAUGE_ACTIVE_RUNTIMES: Final = "tth.active_runtimes"
GAUGE_ACTIVE_TURNS: Final = "tth.active_turns"
GAUGE_WAITING_INTERACTIONS: Final = "tth.waiting_interactions"
GAUGE_WORKER_READY: Final = "tth.worker_ready"

GAUGE_SAMPLE_NAMES: Final[frozenset[str]] = frozenset(
    {
        GAUGE_ACCEPTED_QUEUE_DEPTH,
        GAUGE_OWNED_CONVERSATIONS,
        GAUGE_ACTIVE_RUNTIMES,
        GAUGE_ACTIVE_TURNS,
        GAUGE_WAITING_INTERACTIONS,
        GAUGE_WORKER_READY,
    }
)

HIST_COMMAND_QUEUE_DELAY: Final = "tth.command_queue_delay"
HIST_RUNTIME_START_RESUME_DURATION: Final = "tth.runtime_start_resume_duration"
HIST_STARTUP_RECOVERY_DURATION: Final = "tth.startup_recovery_duration"
HIST_TURN_DURATION: Final = "tth.turn_duration"
HIST_INTERACTION_WAIT_DURATION: Final = "tth.interaction_wait_duration"
HIST_SHUTDOWN_DURATION: Final = "tth.shutdown_duration"
HIST_TOKEN_COST: Final = "tth.token_cost"

_AttrValue = str | StrEnum


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(INSTRUMENTATION_SCOPE)


def get_meter() -> metrics.Meter:
    return metrics.get_meter(INSTRUMENTATION_SCOPE)


def _enum_value(value: _AttrValue) -> str:
    return value.value if isinstance(value, StrEnum) else value


_TYPED_ATTR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "harness_kind",
        "command_kind",
        "operation",
        "outcome",
        "error_code",
        "recovery_trigger",
        "recovery_action",
        "process_status",
        "interaction_kind",
        "tool_outcome",
        "transport",
        "database_system",
    }
)


def _build_attributes(**typed: _AttrValue | None) -> Attributes:
    """Map typed keyword fields onto the allowlisted attribute keys."""
    unknown = set(typed) - _TYPED_ATTR_KEYS
    if unknown:
        raise ValueError(f"unknown attribute fields: {sorted(unknown)}")
    mapping: dict[str, _AttrValue | None] = {
        ATTR_HARNESS_KIND: typed.get("harness_kind"),
        ATTR_COMMAND_KIND: typed.get("command_kind"),
        ATTR_OPERATION: typed.get("operation"),
        ATTR_OUTCOME: typed.get("outcome"),
        ATTR_ERROR_CODE: typed.get("error_code"),
        ATTR_RECOVERY_TRIGGER: typed.get("recovery_trigger"),
        ATTR_RECOVERY_ACTION: typed.get("recovery_action"),
        ATTR_PROCESS_STATUS: typed.get("process_status"),
        ATTR_INTERACTION_KIND: typed.get("interaction_kind"),
        ATTR_TOOL_OUTCOME: typed.get("tool_outcome"),
        ATTR_TRANSPORT: typed.get("transport"),
        ATTR_DATABASE_SYSTEM: typed.get("database_system"),
    }
    out: dict[str, str] = {}
    for key, raw in mapping.items():
        if raw is None:
            continue
        out[key] = _enum_value(raw)
    return out


class Observability:
    """Process-local instruments and typed recording helpers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gauge_samples: dict[str, float] = {name: 0.0 for name in GAUGE_SAMPLE_NAMES}
        meter = get_meter()
        self._commands = meter.create_counter(
            METRIC_COMMANDS,
            description="Commands by kind and outcome",
            unit="1",
        )
        self._recovery_attempts = meter.create_counter(
            METRIC_RECOVERY_ATTEMPTS,
            description="Recovery attempts by trigger, action, and outcome",
            unit="1",
        )
        self._process_exits = meter.create_counter(
            METRIC_PROCESS_EXITS,
            description="Process exits by status",
            unit="1",
        )
        self._stderr_truncations = meter.create_counter(
            METRIC_STDERR_TRUNCATIONS,
            description="Stderr truncations",
            unit="1",
        )
        self._tool_terminal_outcomes = meter.create_counter(
            METRIC_TOOL_TERMINAL_OUTCOMES,
            description="Tool terminal outcomes",
            unit="1",
        )
        self._interaction_requests = meter.create_counter(
            METRIC_INTERACTION_REQUESTS,
            description="Interaction requests by kind",
            unit="1",
        )
        self._interaction_resolutions = meter.create_counter(
            METRIC_INTERACTION_RESOLUTIONS,
            description="Interaction resolutions by kind and outcome",
            unit="1",
        )
        self._sse_reconnects = meter.create_counter(
            METRIC_SSE_RECONNECTS,
            description="SSE reconnects",
            unit="1",
        )
        self._usage_observations = meter.create_counter(
            METRIC_USAGE_OBSERVATIONS,
            description="Committed usage observations",
            unit="1",
        )
        self._cost_observations = meter.create_counter(
            METRIC_COST_OBSERVATIONS,
            description="Committed cost observations",
            unit="1",
        )
        self._command_queue_delay = meter.create_histogram(
            HIST_COMMAND_QUEUE_DELAY,
            description="Command queue delay",
            unit="s",
        )
        self._runtime_start_resume_duration = meter.create_histogram(
            HIST_RUNTIME_START_RESUME_DURATION,
            description="Runtime start or resume duration",
            unit="s",
        )
        self._startup_recovery_duration = meter.create_histogram(
            HIST_STARTUP_RECOVERY_DURATION,
            description="Startup recovery duration",
            unit="s",
        )
        self._turn_duration = meter.create_histogram(
            HIST_TURN_DURATION,
            description="Turn duration",
            unit="s",
        )
        self._interaction_wait_duration = meter.create_histogram(
            HIST_INTERACTION_WAIT_DURATION,
            description="Interaction wait duration",
            unit="s",
        )
        self._shutdown_duration = meter.create_histogram(
            HIST_SHUTDOWN_DURATION,
            description="Shutdown duration",
            unit="s",
        )
        self._token_cost = meter.create_histogram(
            HIST_TOKEN_COST,
            description="Reported token or cost values",
            unit="1",
        )
        self._register_gauges(meter)

    def _register_gauges(self, meter: metrics.Meter) -> None:
        for name in GAUGE_SAMPLE_NAMES:
            meter.create_observable_gauge(
                name,
                callbacks=[self._gauge_callback(name)],
                description=f"Process-local sample for {name}",
                unit="1",
            )

    def _gauge_callback(self, name: str):
        def _callback(_options: CallbackOptions) -> Iterator[Observation]:
            with self._lock:
                value = self._gauge_samples.get(name, 0.0)
            yield Observation(value)

        return _callback

    def set_gauge_sample(self, name: str, value: float | int | bool) -> None:
        if name not in GAUGE_SAMPLE_NAMES:
            raise ValueError(f"unknown gauge sample: {name}")
        with self._lock:
            self._gauge_samples[name] = float(value)

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        harness_kind: HarnessKind | str | None = None,
        command_kind: CommandKind | str | None = None,
        operation: str | None = None,
        outcome: str | None = None,
        error_code: ErrorCode | str | None = None,
        recovery_trigger: RecoveryTrigger | str | None = None,
        recovery_action: RecoveryAction | str | None = None,
        process_status: ProcessStatus | str | None = None,
        interaction_kind: InteractionKind | str | None = None,
        tool_outcome: ToolOutcome | str | None = None,
        transport: str | None = None,
        database_system: str | None = None,
    ) -> Generator[Span]:
        if name not in SPAN_NAMES:
            raise ValueError(f"unknown span name: {name}")
        attrs = _build_attributes(
            harness_kind=harness_kind,
            command_kind=command_kind,
            operation=operation,
            outcome=outcome,
            error_code=error_code,
            recovery_trigger=recovery_trigger,
            recovery_action=recovery_action,
            process_status=process_status,
            interaction_kind=interaction_kind,
            tool_outcome=tool_outcome,
            transport=transport,
            database_system=database_system,
        )
        with get_tracer().start_as_current_span(name, attributes=attrs) as span:
            yield span

    def mark_span_error(self, span: Span, error_code: ErrorCode | str) -> None:
        """Mark span status with a fixed error code only — never record_exception."""
        code = _enum_value(error_code)
        span.set_attribute(ATTR_ERROR_CODE, code)
        span.set_status(Status(StatusCode.ERROR, code))

    def record_command(
        self,
        *,
        kind: CommandKind | str,
        outcome: str,
        harness_kind: HarnessKind | str | None = None,
    ) -> None:
        attrs = _build_attributes(
            command_kind=kind,
            outcome=outcome,
            harness_kind=harness_kind,
        )
        self._commands.add(1, attributes=attrs)

    def record_recovery(
        self,
        *,
        trigger: RecoveryTrigger | str,
        action: RecoveryAction | str,
        outcome: str,
        error_code: ErrorCode | str | None = None,
    ) -> None:
        attrs = _build_attributes(
            recovery_trigger=trigger,
            recovery_action=action,
            outcome=outcome,
            error_code=error_code,
        )
        self._recovery_attempts.add(1, attributes=attrs)

    def record_process_exit(self, *, status: ProcessStatus | str) -> None:
        self._process_exits.add(1, attributes=_build_attributes(process_status=status))

    def record_stderr_truncation(self) -> None:
        self._stderr_truncations.add(1)

    def record_tool_terminal_outcome(self, *, outcome: ToolOutcome | str) -> None:
        self._tool_terminal_outcomes.add(1, attributes=_build_attributes(tool_outcome=outcome))

    def record_interaction_request(self, *, kind: InteractionKind | str) -> None:
        self._interaction_requests.add(1, attributes=_build_attributes(interaction_kind=kind))

    def record_interaction_resolution(
        self,
        *,
        kind: InteractionKind | str,
        outcome: str,
    ) -> None:
        self._interaction_resolutions.add(
            1,
            attributes=_build_attributes(interaction_kind=kind, outcome=outcome),
        )

    def record_sse_reconnect(self, *, transport: str = "sse") -> None:
        self._sse_reconnects.add(1, attributes=_build_attributes(transport=transport))

    def record_usage_observation(self) -> None:
        self._usage_observations.add(1)

    def record_cost_observation(self) -> None:
        self._cost_observations.add(1)

    def record_command_queue_delay(self, seconds: float) -> None:
        self._command_queue_delay.record(seconds)

    def record_runtime_start_resume_duration(
        self,
        seconds: float,
        *,
        operation: str,
        harness_kind: HarnessKind | str | None = None,
    ) -> None:
        attrs = _build_attributes(operation=operation, harness_kind=harness_kind)
        self._runtime_start_resume_duration.record(seconds, attributes=attrs)

    def record_startup_recovery_duration(
        self,
        seconds: float,
        *,
        database_system: str | None = None,
    ) -> None:
        attrs = _build_attributes(database_system=database_system)
        self._startup_recovery_duration.record(seconds, attributes=attrs)

    def record_turn_duration(self, seconds: float) -> None:
        self._turn_duration.record(seconds)

    def record_interaction_wait_duration(self, seconds: float) -> None:
        self._interaction_wait_duration.record(seconds)

    def record_shutdown_duration(self, seconds: float) -> None:
        self._shutdown_duration.record(seconds)

    def record_token_cost(self, value: float) -> None:
        self._token_cost.record(value)

    def observe_committed_events(
        self,
        events: Sequence[ConversationEvent],
        *,
        state: ConversationState | None = None,
    ) -> None:
        """Record canonical behavior exactly after its persistence commit."""
        for event in events:
            payload = event.payload
            if isinstance(payload, ProcessStderrTruncatedPayload):
                self.record_stderr_truncation()
            elif isinstance(payload, ProcessExitedPayload):
                status = (
                    ProcessStatus.EXITED if payload.exit_code in (0, None) else ProcessStatus.FAILED
                )
                self.record_process_exit(status=status)
            elif isinstance(payload, ProcessForcedTerminationPayload):
                self.record_process_exit(status=ProcessStatus.TERMINATED)
            elif isinstance(payload, ToolCompletedPayload):
                self.record_tool_terminal_outcome(outcome=payload.outcome)
            elif isinstance(payload, ToolFailedPayload):
                self.record_tool_terminal_outcome(outcome=ToolOutcome.FAILURE)
            elif isinstance(payload, InteractionRequestedPayload):
                self.record_interaction_request(kind=payload.kind)
            elif isinstance(payload, InteractionResolvedPayload) and state is not None:
                interaction = state.interactions.get(payload.interaction_id)
                if interaction is not None:
                    self.record_interaction_resolution(
                        kind=interaction.kind,
                        outcome=(payload.decision.value if payload.decision else "answered"),
                    )
            elif isinstance(payload, UsageUpdatedPayload):
                self.record_usage_observation()
                if payload.total_tokens is not None:
                    self.record_token_cost(float(payload.total_tokens))
            elif isinstance(payload, CostUpdatedPayload):
                self.record_cost_observation()
                self.record_token_cost(float(payload.cost))


_observability: Observability | None = None
_observability_lock = threading.Lock()


def get_observability() -> Observability:
    """Return the process-local Observability singleton."""
    global _observability
    if _observability is None:
        with _observability_lock:
            if _observability is None:
                _observability = Observability()
    return _observability


def reset_observability_for_tests() -> None:
    """Drop the singleton so the next call rebuilds instruments (tests only)."""
    global _observability
    with _observability_lock:
        _observability = None
