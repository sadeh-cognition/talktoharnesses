"""Opt-out OpenTelemetry traces, metrics, and logs for this split service.

Telemetry exports by default. Set ``OTEL_EXPORTER_OTLP_ENDPOINT`` to ``false``
or ``0`` (case-insensitive) to disable every signal; any other value is used
as the OTLP/HTTP endpoint, and unset falls back to the SDK default
(``http://localhost:4318``). Because export is on by default, the SDK and
exporter packages are regular dependencies; if they are missing and telemetry
is not opted out, startup fails.
"""

from __future__ import annotations

import logging
import os
import sys

_SERVICE_NAME = "tth-grok"
_OTEL_OPT_OUT_VALUES = frozenset({"false", "0"})

_telemetry_enabled = False


def telemetry_opted_out() -> bool:
    raw = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    return raw is not None and raw.strip().lower() in _OTEL_OPT_OUT_VALUES


class _ExcludeOpenTelemetryRecords(logging.Filter):
    """OTel's own loggers must not feed back into the OTLP log exporter:
    an export failure would otherwise log an error that is itself exported,
    creating a loop while the collector is unreachable."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry")


def configure_opentelemetry(*, log_level: str = "INFO") -> None:
    """Set up OTLP-exporting tracer, meter, and logger providers."""
    global _telemetry_enabled
    if _telemetry_enabled or telemetry_opted_out():
        return

    try:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
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
            "packages are missing; rebuild the image / reinstall dependencies "
            "or set OTEL_EXPORTER_OTLP_ENDPOINT=false to opt out"
        ) from exc

    resource = Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME") or _SERVICE_NAME}
    )

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

    otel_handler = LoggingHandler(logger_provider=logger_provider)
    otel_handler.addFilter(_ExcludeOpenTelemetryRecords())
    root_logger = logging.getLogger()
    root_logger.addHandler(otel_handler)
    # With no root handlers, stdlib WARNING+ reached stderr via
    # logging.lastResort; adding a handler silences that fallback, so
    # restore the stderr visibility explicitly.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    root_logger.addHandler(stderr_handler)
    root_logger.setLevel(log_level)

    try:
        # Only the splits that ship httpx carry its instrumentation package;
        # the guard keeps this module identical across all six splits.
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # pyright: ignore
    except ImportError:
        pass
    else:
        HTTPXClientInstrumentor().instrument()  # pyright: ignore

    _telemetry_enabled = True


def instrument_django() -> None:
    """Insert the OTel request middleware for HTTP server spans and metrics.

    Must run after settings are loaded but before the ASGI handler is
    built, because ``DjangoInstrumentor`` inserts into ``settings.MIDDLEWARE``.
    """
    if not _telemetry_enabled:
        return

    from opentelemetry.instrumentation.django import DjangoInstrumentor  # pyright: ignore

    DjangoInstrumentor().instrument()
