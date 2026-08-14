"""HTTP request/response logging via loguru."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse
from loguru import logger

LOG_FILE_NAME = "talktoharnesses.log"
DEFAULT_LOG_LEVEL = "DEBUG"


def configure_logging(
    *,
    level: str = DEFAULT_LOG_LEVEL,
    log_file: str | Path | None = None,
) -> None:
    """Send loguru output to stderr and a cwd-relative log file."""
    path = Path(log_file) if log_file is not None else Path.cwd() / LOG_FILE_NAME
    logger.remove()
    logger.add(sys.stderr, level=level)
    logger.add(str(path), level=level, enqueue=True, rotation="10 MB", retention=5)


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
