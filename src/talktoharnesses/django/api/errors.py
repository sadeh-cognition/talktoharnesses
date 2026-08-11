"""Map domain failures to stable HTTP responses at the Ninja boundary."""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

from talktoharnesses.django.auth import (
    AUTH_FAILURE_CODE,
    AUTH_FAILURE_MESSAGE,
    AuthenticationFailed,
)
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError, public_message
from talktoharnesses.domain.models import ErrorProjection

logger = logging.getLogger(__name__)

_CONFLICT_CODES = frozenset(
    {
        ErrorCode.CONVERSATION_BUSY,
        ErrorCode.IDEMPOTENCY_CONFLICT,
        ErrorCode.HARNESS_IN_USE,
        ErrorCode.OPTIMISTIC_CONFLICT,
        ErrorCode.INTERACTION_ALREADY_RESOLVED,
        ErrorCode.PROVIDER_INCOMPATIBLE,
        ErrorCode.MODE_CHANGE_WHILE_ACTIVE,
        ErrorCode.QUEUED_PROMPT_NOT_EDITABLE,
        ErrorCode.NO_ACTIVE_TURN,
        ErrorCode.NO_QUEUED_PROMPT,
    }
)

_BAD_REQUEST_CODES = frozenset(
    {
        ErrorCode.INVALID_SEARCH_QUERY,
    }
)

_UNPROCESSABLE_CODES = frozenset(
    {
        ErrorCode.INVALID_CURSOR,
        ErrorCode.INVALID_STATE,  # blank idempotency, bad page limit, etc.
    }
)


def _json_error(code: str, message: str, status: int) -> HttpResponse:
    body = ErrorProjection(code=code, message=message).model_dump_json()
    response = HttpResponse(body, status=status, content_type="application/json")
    if status == 401:
        response["WWW-Authenticate"] = "Bearer"
    return response


def domain_error_response(exc: DomainError) -> HttpResponse:
    """Map a DomainError to a stable HTTP body without echoing raw messages."""
    if isinstance(exc, AuthenticationFailed):
        return _json_error(AUTH_FAILURE_CODE, AUTH_FAILURE_MESSAGE, 401)
    if exc.code is ErrorCode.NOT_FOUND:
        return _json_error(exc.code.value, public_message(exc.code), 404)
    # get_snapshot still uses INVALID_STATE for missing/owner mismatch.
    # Inspect the internal message only for status routing — never echo it.
    if exc.code is ErrorCode.INVALID_STATE and "not found" in exc.message.lower():
        return _json_error(ErrorCode.NOT_FOUND.value, public_message(ErrorCode.NOT_FOUND), 404)
    if exc.code is ErrorCode.INVALID_STATE and "owner mismatch" in exc.message.lower():
        return _json_error(ErrorCode.NOT_FOUND.value, public_message(ErrorCode.NOT_FOUND), 404)
    if exc.code in _BAD_REQUEST_CODES:
        return _json_error(exc.code.value, public_message(exc.code), 400)
    if exc.code in _CONFLICT_CODES:
        return _json_error(exc.code.value, public_message(exc.code), 409)
    if exc.code in _UNPROCESSABLE_CODES or exc.code is ErrorCode.INVALID_CURSOR:
        return _json_error(exc.code.value, public_message(exc.code), 422)
    return _json_error(exc.code.value, public_message(exc.code), 409)


def register_exception_handlers(api: NinjaAPI) -> None:
    def on_auth_failed(request: HttpRequest, exc: AuthenticationFailed) -> HttpResponse:
        return _json_error(AUTH_FAILURE_CODE, AUTH_FAILURE_MESSAGE, 401)

    def on_ninja_auth(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
        return _json_error(AUTH_FAILURE_CODE, AUTH_FAILURE_MESSAGE, 401)

    def on_domain(request: HttpRequest, exc: DomainError) -> HttpResponse:
        return domain_error_response(exc)

    def on_validation(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        return _json_error("validation_error", "invalid request", 422)

    def on_http(request: HttpRequest, exc: HttpError) -> HttpResponse:
        if exc.status_code == 401:
            return _json_error(AUTH_FAILURE_CODE, AUTH_FAILURE_MESSAGE, 401)
        return _json_error("http_error", "http error", exc.status_code)

    def on_unexpected(request: HttpRequest, exc: Exception) -> HttpResponse:
        logger.exception("unhandled API error")
        return _json_error("internal_error", "internal server error", 500)

    # Ninja ExcHandler typing is overly strict about exception class unions.
    api.add_exception_handler(AuthenticationFailed, on_auth_failed)  # type: ignore[arg-type]
    api.add_exception_handler(AuthenticationError, on_ninja_auth)  # type: ignore[arg-type]
    api.add_exception_handler(DomainError, on_domain)  # type: ignore[arg-type]
    api.add_exception_handler(ValidationError, on_validation)  # type: ignore[arg-type]
    api.add_exception_handler(HttpError, on_http)  # type: ignore[arg-type]
    api.add_exception_handler(Exception, on_unexpected)  # type: ignore[arg-type]
