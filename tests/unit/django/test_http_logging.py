"""ASGI-safe request/response logging middleware."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from talktoharnesses.django import http_logging
from talktoharnesses.django.http_logging import (
    RequestResponseLoggingMiddleware,
    configure_logging,
)


def test_sync_get_response_returns_http_response() -> None:
    def get_response(request: HttpRequest) -> HttpResponse:
        return HttpResponse(b"ok")

    middleware = RequestResponseLoggingMiddleware(get_response)
    response = middleware(RequestFactory().get("/api/v1/harnesses?limit=100"))
    assert isinstance(response, HttpResponse)
    assert response.content == b"ok"


async def test_async_get_response_is_awaitable_like_django_asgi() -> None:
    async def get_response(request: HttpRequest) -> HttpResponse:
        return HttpResponse(b"ok")

    middleware = RequestResponseLoggingMiddleware(get_response)
    result = middleware(RequestFactory().get("/api/v1/harnesses?limit=100"))
    assert not isinstance(result, HttpResponse)
    response = await result
    assert response.content == b"ok"


def test_request_and_response_bodies_are_not_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_response(request: HttpRequest) -> HttpResponse:
        del request
        return HttpResponse(b'{"access":"live-jwt"}')

    debug = Mock()
    monkeypatch.setattr(http_logging.logger, "debug", debug)
    middleware = RequestResponseLoggingMiddleware(get_response)
    middleware(RequestFactory().post("/api/v1/auth/token/rotate", {"refresh": "secret"}))

    logged = repr(debug.call_args_list)
    assert "secret" not in logged
    assert "live-jwt" not in logged
    assert "body=" not in logged


def test_file_logging_has_bounded_rotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    add = Mock(side_effect=[1, 2])
    monkeypatch.setattr(http_logging.logger, "remove", Mock())
    monkeypatch.setattr(http_logging.logger, "add", add)

    configure_logging(log_file=tmp_path / "requests.log")

    file_options = add.call_args_list[1].kwargs
    assert file_options["rotation"] == "10 MB"
    assert file_options["retention"] == 5
