"""Opt-out OpenTelemetry traces, metrics, and logs for the TTH host process.

Telemetry exports by default. Set ``OTEL_EXPORTER_OTLP_ENDPOINT`` to ``false``
or ``0`` (case-insensitive) to disable every signal; any other value is used
as the OTLP/HTTP endpoint, and unset falls back to the SDK default
(``http://localhost:4318``). Because export is on by default, the SDK,
exporter, and instrumentation packages must be installed (the root dev
dependency group carries them); if they are missing and telemetry is not
opted out, startup fails.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loguru import Record

_OTEL_OPT_OUT_VALUES = frozenset({"false", "0"})

_telemetry_enabled = False


def telemetry_opted_out() -> bool:
    raw = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    return raw is not None and raw.strip().lower() in _OTEL_OPT_OUT_VALUES


def _exclude_opentelemetry_records(record: Record) -> bool:
    # OTel's own loggers must not feed back into the OTLP log exporter:
    # an export failure would otherwise log an error that is itself exported,
    # creating a loop while the collector is unreachable.
    name = record["name"]
    return name is None or not name.startswith("opentelemetry")


class _ExcludeOpenTelemetryLogRecords(logging.Filter):
    """The same feedback-loop guard for the stdlib root-logger handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry")


def configure_opentelemetry(service_name: str | None = None, *, log_level: str = "INFO") -> None:
    """Set up OTLP-exporting tracer, meter, and logger providers."""
    global _telemetry_enabled
    if _telemetry_enabled or telemetry_opted_out():
        return

    try:
        from loguru import logger as loguru_logger
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise RuntimeError(
            "OpenTelemetry export is enabled by default but the SDK/exporter "
            "packages are not installed; run `uv sync` or set "
            "OTEL_EXPORTER_OTLP_ENDPOINT=false to opt out"
        ) from exc

    resolved_name = service_name or os.environ.get("OTEL_SERVICE_NAME") or "talktoharnesses"
    resource = Resource.create({"service.name": resolved_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    # No enqueue on this sink: the handler must capture the active span
    # context at emit time to correlate logs with traces.
    loguru_logger.add(
        LoggingHandler(logger_provider=logger_provider),
        level=log_level,
        filter=_exclude_opentelemetry_records,
        format="{message}",
    )

    # Library modules log through stdlib logging, which loguru never sees;
    # export those records via a root-logger handler as well.
    otel_handler = LoggingHandler(logger_provider=logger_provider)
    otel_handler.addFilter(_ExcludeOpenTelemetryLogRecords())
    root_logger = logging.getLogger()
    root_logger.addHandler(otel_handler)
    # With no root handlers, stdlib WARNING+ reached stderr via
    # logging.lastResort; adding a handler silences that fallback, so
    # restore the stderr visibility explicitly.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    root_logger.addHandler(stderr_handler)
    root_logger.setLevel(log_level)

    HTTPXClientInstrumentor().instrument()

    _telemetry_enabled = True


def instrument_django() -> None:
    """Insert the OTel request middleware for HTTP server spans and metrics.

    Must run after settings are loaded but before the ASGI handler is
    built, because ``DjangoInstrumentor`` inserts into ``settings.MIDDLEWARE``.
    """
    if not _telemetry_enabled:
        return

    from opentelemetry.instrumentation.django import DjangoInstrumentor

    DjangoInstrumentor().instrument()
