"""Secret-safe DomainError HTTP mapping (Phase 9 WP4)."""

from __future__ import annotations

import json

from talktoharnesses.django.api.errors import domain_error_response
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError, public_message


def test_domain_error_does_not_echo_scary_message() -> None:
    secret = "SECRET_TOKEN=sk-live-provider-dump argv=['rm','-rf']"
    response = domain_error_response(DomainError(ErrorCode.PROTOCOL_ERROR, secret))
    body = json.loads(response.content)
    assert response.status_code == 409
    assert body["code"] == ErrorCode.PROTOCOL_ERROR.value
    assert body["message"] == public_message(ErrorCode.PROTOCOL_ERROR)
    assert secret not in body["message"]
    assert "SECRET" not in response.content.decode()
    assert "sk-live" not in response.content.decode()


def test_provider_incompatible_uses_generic_message() -> None:
    response = domain_error_response(
        DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "adapter said: /home/user/.env leaked")
    )
    body = json.loads(response.content)
    assert body["code"] == ErrorCode.PROVIDER_INCOMPATIBLE.value
    assert body["message"] == public_message(ErrorCode.PROVIDER_INCOMPATIBLE)
    assert "/home/user" not in body["message"]


def test_provider_version_mismatch_includes_safe_versions() -> None:
    response = domain_error_response(
        DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "unknown grok release",
            details={
                "provider": "Grok",
                "installed_version": "1.0.3 (1a29d5bc12) [stable]",
                "supported_versions": ["1.0.0 (3cd0d0cbce) [stable]"],
            },
        )
    )

    assert json.loads(response.content) == {
        "code": ErrorCode.PROVIDER_INCOMPATIBLE.value,
        "message": (
            "Grok version 1.0.3 (1a29d5bc12) [stable] is incompatible; "
            "supported versions: 1.0.0 (3cd0d0cbce) [stable]"
        ),
    }


def test_provider_version_mismatch_does_not_echo_untrusted_values() -> None:
    response = domain_error_response(
        DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "unknown release",
            details={
                "provider": "Grok",
                "installed_version": "1.0.3; SECRET_TOKEN=sk-live",
                "supported_versions": ["1.0.0"],
            },
        )
    )

    body = json.loads(response.content)
    assert body["message"] == public_message(ErrorCode.PROVIDER_INCOMPATIBLE)
    assert "SECRET" not in body["message"]


def test_not_found_and_auth_remain_stable() -> None:
    missing = domain_error_response(DomainError(ErrorCode.NOT_FOUND, "conversation xyz missing"))
    assert missing.status_code == 404
    assert json.loads(missing.content) == {
        "code": "not_found",
        "message": "not found",
    }

    owner = domain_error_response(
        DomainError(ErrorCode.INVALID_STATE, "owner mismatch for conversation")
    )
    assert owner.status_code == 404
    assert json.loads(owner.content)["message"] == "not found"
