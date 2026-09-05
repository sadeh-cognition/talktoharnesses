"""Map DomainError to SplitError HTTP responses.

Unlike the proxy's client-facing boundary, the proxy is a trusted internal
client: the raw ``DomainError.message`` and full details are sent so the
proxy can re-raise a lossless DomainError (its recovery logic inspects
messages). The proxy's own API boundary still sanitizes before clients see it.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError
from pydantic import ValidationError as PydanticValidationError
from tth_types.enums import ErrorCode
from tth_types.errors import DomainError
from tth_types.split_api import SplitError

logger = logging.getLogger(__name__)

_CONFLICT_CODES = frozenset(
    {
        ErrorCode.CONVERSATION_BUSY,
        ErrorCode.PROVIDER_INCOMPATIBLE,
        ErrorCode.INVALID_EXECUTABLE,
        ErrorCode.EXECUTABLE_OWNER_MISMATCH,
        ErrorCode.WORKING_DIRECTORY_NOT_FOUND,
        ErrorCode.WORKSPACE_ROOT_NOT_FOUND,
        ErrorCode.RUNTIME_TIMEOUT,
    }
)


def _json_error(
    code: str,
    message: str,
    status: int,
    details: dict[str, object] | None = None,
) -> HttpResponse:
    body = SplitError(code=code, message=message, details=dict(details or {})).model_dump_json()
    response = HttpResponse(body, status=status, content_type="application/json")
    if status == 401:
        response["WWW-Authenticate"] = "Bearer"
    return response


def domain_error_response(exc: DomainError) -> HttpResponse:
    if exc.code is ErrorCode.NOT_FOUND:
        status = 404
    elif exc.code is ErrorCode.INVALID_STATE:
        status = 422
    elif exc.code in _CONFLICT_CODES:
        status = 409
    else:
        status = 409
    return _json_error(exc.code.value, exc.message, status, exc.details)


def register_exception_handlers(api: NinjaAPI) -> None:
    def on_auth(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
        return _json_error("authentication_failed", "authentication failed", 401)

    def on_domain(request: HttpRequest, exc: DomainError) -> HttpResponse:
        return domain_error_response(exc)

    def on_validation(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        return _json_error("validation_error", "invalid request", 422)

    def on_pydantic(request: HttpRequest, exc: PydanticValidationError) -> HttpResponse:
        return _json_error("validation_error", str(exc), 422)

    def on_http(request: HttpRequest, exc: HttpError) -> HttpResponse:
        if exc.status_code == 401:
            return _json_error("authentication_failed", "authentication failed", 401)
        return _json_error("http_error", str(exc), exc.status_code)

    def on_unexpected(request: HttpRequest, exc: Exception) -> HttpResponse:
        logger.exception("unhandled split API error")
        return _json_error("internal_error", "internal server error", 500)

    # Ninja ExcHandler typing is overly strict about exception class unions.
    api.add_exception_handler(AuthenticationError, on_auth)  # type: ignore[arg-type]
    api.add_exception_handler(DomainError, on_domain)  # type: ignore[arg-type]
    api.add_exception_handler(ValidationError, on_validation)  # type: ignore[arg-type]
    api.add_exception_handler(PydanticValidationError, on_pydantic)  # type: ignore[arg-type]
    api.add_exception_handler(HttpError, on_http)  # type: ignore[arg-type]
    api.add_exception_handler(Exception, on_unexpected)  # type: ignore[arg-type]
