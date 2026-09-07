"""HTTP request/response logging via loguru."""

from __future__ import annotations

import inspect
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse
from loguru import logger

LOG_FILE_NAME = "talktoharnesses.log"
# One level governs every sink. Loguru-native records (this module) and the
# stdlib records of the proxy's own modules (runtime manager, command
# processor, remote adapter, sandbox) both honour it; without the intercept
# below only WARNING+ stdlib records reached stderr via ``logging.lastResort``
# and nothing reached the log file, which hid the worker-side story of a
# stalled turn.
DEFAULT_LOG_LEVEL = "DEBUG"
LOG_LEVEL_ENV = "TTH_LOG_LEVEL"
# Third-party loggers never go below WARNING, whatever the level above says.
NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "docker", "uvicorn.access")
# Timestamps carry an explicit UTC marker so they line up with agentbahn's
# records and the harness journals regardless of the process time zone.
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DDTHH:mm:ss.SSS!UTC}Z</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

# Attributes every LogRecord carries; anything else was passed via ``extra=``.
_STD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class InterceptHandler(logging.Handler):
    """Forward stdlib ``logging`` records into loguru's sinks."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk out of the logging module so loguru attributes the record to
        # the stdlib caller (loguru's documented InterceptHandler recipe).
        frame, depth = inspect.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        # Fields passed as ``extra=`` become loguru ``extra`` so the file and
        # exporter sinks keep them as attributes instead of message substrings.
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_RECORD_ATTRS}
        # ``stdlib_intercept`` lets sinks that already receive stdlib records
        # through their own root handler (the OTel exporter) skip the copy.
        logger.bind(stdlib_intercept=True, **extras).opt(
            depth=depth, exception=record.exc_info
        ).log(level, record.getMessage())


def configure_logging(
    *,
    level: str | None = None,
    log_file: str | Path | None = None,
) -> None:
    """Send loguru output to stderr and a cwd-relative log file.

    ``level`` (default ``TTH_LOG_LEVEL``, then ``DEBUG``) applies to both
    sinks and to the stdlib records routed into them.
    """
    resolved = (level or os.environ.get(LOG_LEVEL_ENV) or DEFAULT_LOG_LEVEL).upper()
    path = Path(log_file) if log_file is not None else Path.cwd() / LOG_FILE_NAME
    logger.remove()
    logger.add(sys.stderr, level=resolved, format=LOG_FORMAT)
    logger.add(
        str(path),
        level=resolved,
        format=LOG_FORMAT,
        enqueue=True,
        rotation="10 MB",
        retention=5,
    )
    intercept_stdlib_logging(resolved)


def intercept_stdlib_logging(level: str = DEFAULT_LOG_LEVEL) -> None:
    """Install the loguru intercept on the root stdlib logger (idempotent)."""
    root = logging.getLogger()
    if not any(isinstance(handler, InterceptHandler) for handler in root.handlers):
        root.addHandler(InterceptHandler())
    root.setLevel(level.upper())
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def stdlib_logging_intercepted() -> bool:
    return any(isinstance(h, InterceptHandler) for h in logging.getLogger().handlers)


def _log_request(request: HttpRequest) -> None:
    logger.debug("request {} {}", request.method, request.get_full_path())


def _log_response(request: HttpRequest, response: HttpResponse, started: float) -> None:
    elapsed_ms = (perf_counter() - started) * 1000
    logger.debug(
        "response {} {} status={} elapsed_ms={:.1f}",
        request.method,
        request.get_full_path(),
        response.status_code,
        elapsed_ms,
    )


class RequestResponseLoggingMiddleware:
    """Log each HTTP request and its response at DEBUG."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], Any]) -> None:
        self.get_response = get_response
        # ASGI awaits this instance only after markcoroutinefunction (Django MiddlewareMixin).
        self.async_mode = iscoroutinefunction(self.get_response)  # pyright: ignore[reportDeprecated]
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> HttpResponse | Awaitable[HttpResponse]:
        if self.async_mode:
            return self.__acall__(request)
        started = perf_counter()
        _log_request(request)
        response = self.get_response(request)
        _log_response(request, response, started)
        return response

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        started = perf_counter()
        _log_request(request)
        response = await self.get_response(request)
        _log_response(request, response, started)
        return response
