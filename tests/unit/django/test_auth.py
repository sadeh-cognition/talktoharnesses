"""JWT issuance, authentication, rotation, and revocation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from django.contrib.auth import get_user_model

from talktoharnesses.django.auth import (
    AUTH_FAILURE_MESSAGE,
    JWT_AUDIENCE,
    JWT_ISSUER,
    AuthenticationFailed,
    authenticate_bearer_sync,
    issue_token_sync,
    owner_id_for_user,
    revoke_token_sync,
    rotate_token_sync,
    validate_jwt_settings,
)
from talktoharnesses.django.models import ApiToken
from talktoharnesses.domain import DomainError

_jwt: Any = jwt


@pytest.fixture
def user(db: Any) -> Any:
    User: Any = get_user_model()
    return User.objects.create_user(username="alice", password="x")


@pytest.fixture
def inactive_user(db: Any) -> Any:
    User: Any = get_user_model()
    u = User.objects.create_user(username="bob", password="x")
    u.is_active = False
    u.save(update_fields=("is_active",))
    return u


def test_validate_jwt_settings_rejects_bad_keys(settings: Any) -> None:
    with pytest.raises(DomainError):
        validate_jwt_settings(signing_key="")
    with pytest.raises(DomainError):
        validate_jwt_settings(signing_key="short")
    with pytest.raises(DomainError):
        validate_jwt_settings(
            signing_key=settings.SECRET_KEY,
            secret_key=settings.SECRET_KEY,
        )
    with pytest.raises(DomainError):
        validate_jwt_settings(
            signing_key="x" * 32,
            token_ttl=timedelta(0),
        )
    ok = validate_jwt_settings(signing_key="k" * 32, token_ttl=timedelta(hours=1))
    assert ok.token_ttl == timedelta(hours=1)


@pytest.mark.django_db
def test_issue_token_claims_and_digest_only(user: Any) -> None:
    proj = issue_token_sync(user)
    assert proj.token
    assert proj.expires_at > datetime.now(UTC)

    claims: dict[str, Any] = _jwt.decode(
        proj.token,
        key="test-jwt-signing-key-32-bytes-min!!",
        algorithms=["HS256"],
        audience=JWT_AUDIENCE,
        issuer=JWT_ISSUER,
    )
    assert claims["sub"] == str(user.pk)
    assert claims["iss"] == JWT_ISSUER
    assert claims["aud"] == JWT_AUDIENCE
    assert "jti" in claims
    assert "iat" in claims
    assert "exp" in claims

    row = ApiToken.objects.get(user_id=user.pk)
    digest = hashlib.sha256(claims["jti"].encode()).hexdigest()
    assert row.jti_digest == digest
    # Raw jti must never be stored.
    assert claims["jti"] != row.jti_digest

    assert owner_id_for_user(user) == str(user.pk)


@pytest.mark.django_db
def test_second_issue_invalidates_first(user: Any) -> None:
    first = issue_token_sync(user)
    second = issue_token_sync(user)
    assert first.token != second.token
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(f"Bearer {first.token}")
    auth_user = authenticate_bearer_sync(f"Bearer {second.token}")
    assert auth_user.pk == user.pk


@pytest.mark.django_db
def test_authenticate_rejects_bad_tokens(user: Any) -> None:
    good = issue_token_sync(user)
    cases = [
        None,
        "",
        "Basic abc",
        "Bearer",
        "Bearer ",
        "Bearer not-a-jwt",
        f"Bearer {good.token[:-4]}xxxx",
    ]
    for header in cases:
        with pytest.raises(AuthenticationFailed) as exc:
            authenticate_bearer_sync(header)
        assert exc.value.message == AUTH_FAILURE_MESSAGE


@pytest.mark.django_db
def test_wrong_algorithm_and_claims(user: Any, settings: Any) -> None:
    key = settings.TALKTOHARNESSES_JWT_SIGNING_KEY
    now = datetime.now(UTC)
    # Wrong algorithm (none).
    none_token: str = _jwt.encode(
        {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": str(user.pk),
            "jti": "x",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(f"Bearer {none_token}")

    # Wrong audience.
    bad_aud: str = _jwt.encode(
        {
            "iss": JWT_ISSUER,
            "aud": "other",
            "sub": str(user.pk),
            "jti": "y",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        key,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(f"Bearer {bad_aud}")

    # Expired.
    expired: str = _jwt.encode(
        {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": str(user.pk),
            "jti": "z",
            "iat": int((now - timedelta(days=2)).timestamp()),
            "exp": int((now - timedelta(days=1)).timestamp()),
        },
        key,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(f"Bearer {expired}")


@pytest.mark.django_db
def test_inactive_and_missing_user(user: Any, inactive_user: Any) -> None:
    with pytest.raises(AuthenticationFailed):
        issue_token_sync(inactive_user)

    token = issue_token_sync(user)
    user.is_active = False
    user.save(update_fields=("is_active",))
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(f"Bearer {token.token}")

    # Unknown sub with valid signature.
    key = "test-jwt-signing-key-32-bytes-min!!"
    now = datetime.now(UTC)
    forged: str = _jwt.encode(
        {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": "999999",
            "jti": "ghost",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        key,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(f"Bearer {forged}")


@pytest.mark.django_db
def test_rotate_and_revoke(user: Any) -> None:
    issued = issue_token_sync(user)
    header = f"Bearer {issued.token}"
    rotated = rotate_token_sync(header)
    assert rotated.token != issued.token
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(header)
    new_header = f"Bearer {rotated.token}"
    assert authenticate_bearer_sync(new_header).pk == user.pk

    revoke_token_sync(new_header)
    assert not ApiToken.objects.filter(user_id=user.pk).exists()
    with pytest.raises(AuthenticationFailed):
        authenticate_bearer_sync(new_header)
    with pytest.raises(AuthenticationFailed):
        revoke_token_sync(new_header)


@pytest.mark.django_db
def test_rotation_race_exactly_one_winner(user: Any) -> None:
    """Simulate concurrent rotation: only the first matching digest update wins."""
    issued = issue_token_sync(user)
    header = f"Bearer {issued.token}"
    first = rotate_token_sync(header)
    # Same presented token cannot rotate again after digest was replaced.
    with pytest.raises(AuthenticationFailed):
        rotate_token_sync(header)
    # Only the new token is active.
    assert authenticate_bearer_sync(f"Bearer {first.token}").pk == user.pk
    assert ApiToken.objects.filter(user_id=user.pk).count() == 1
