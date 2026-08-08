"""JWT issuance, validation, rotation, and revocation (Django-only).

Keeps Django user dependencies out of core ``Persistence`` and the facade.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from asgiref.sync import sync_to_async
from django.apps import apps as django_apps
from django.conf import settings
from django.db import transaction

from talktoharnesses.django.models import ApiToken
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import TokenProjection

logger = logging.getLogger(__name__)

JWT_ISSUER = "talktoharnesses"
JWT_AUDIENCE = "talktoharnesses-api"
_JWT_ALGORITHM = "HS256"
_MIN_KEY_BYTES = 32
_DEFAULT_TTL = timedelta(days=30)

# Generic authentication failure used for every bearer path.
AUTH_FAILURE_CODE = "authentication_failed"
AUTH_FAILURE_MESSAGE = "authentication failed"


class AuthenticationFailed(DomainError):
    """Bearer authentication failure (always maps to identical 401)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_STATE, AUTH_FAILURE_MESSAGE)


@dataclass(frozen=True, slots=True)
class JwtSettings:
    signing_key: str
    token_ttl: timedelta


def validate_jwt_settings(
    *,
    signing_key: str | None = None,
    token_ttl: timedelta | None = None,
    secret_key: str | None = None,
) -> JwtSettings:
    """Validate host JWT configuration. Call when constructing the API/ASGI auth surface.

    Does not run on plain persistence app load / management commands.
    """
    if signing_key is not None:
        key = signing_key
    else:
        raw_key = getattr(settings, "TALKTOHARNESSES_JWT_SIGNING_KEY", None)
        if not isinstance(raw_key, str) or not raw_key:
            raise DomainError(
                ErrorCode.INVALID_STATE,
                "TALKTOHARNESSES_JWT_SIGNING_KEY is required",
            )
        key = raw_key
    if not key:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "TALKTOHARNESSES_JWT_SIGNING_KEY is required",
        )
    key_bytes = key.encode("utf-8")
    if len(key_bytes) < _MIN_KEY_BYTES:
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "TALKTOHARNESSES_JWT_SIGNING_KEY must be at least 32 bytes",
        )
    if secret_key is not None:
        django_secret: object = secret_key
    else:
        django_secret = getattr(settings, "SECRET_KEY", None)
    if isinstance(django_secret, str) and hmac.compare_digest(key, django_secret):
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "TALKTOHARNESSES_JWT_SIGNING_KEY must not equal SECRET_KEY",
        )

    if token_ttl is not None:
        ttl = token_ttl
    else:
        configured = getattr(settings, "TALKTOHARNESSES_TOKEN_TTL", None)
        ttl = configured if isinstance(configured, timedelta) else _DEFAULT_TTL
    if ttl <= timedelta(0):
        raise DomainError(
            ErrorCode.INVALID_STATE,
            "TALKTOHARNESSES_TOKEN_TTL must be a positive timedelta",
        )
    return JwtSettings(signing_key=key, token_ttl=ttl)


def _digest(jti: str) -> str:
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _user_model() -> Any:
    return django_apps.get_model(settings.AUTH_USER_MODEL)


def owner_id_for_user(user: Any) -> str:
    """Derive owner_id only from the authenticated user object."""
    return str(user.pk)


def _encode_token(*, user_pk: str, jti: str, issued_at: datetime, expires_at: datetime) -> str:
    cfg = validate_jwt_settings()
    payload: dict[str, Any] = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": user_pk,
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    # PyJWT stubs type the key parameter as partially unknown.
    return jwt.encode(  # pyright: ignore[reportUnknownMemberType]
        payload, cfg.signing_key, algorithm=_JWT_ALGORITHM
    )


def _decode_unverified_claims(token: str) -> dict[str, Any]:
    """Decode and fully validate JWT claims. Raises AuthenticationFailed on any error."""
    try:
        cfg = validate_jwt_settings()
    except DomainError as exc:
        logger.warning("jwt settings invalid during authentication")
        raise AuthenticationFailed() from exc
    try:
        claims: dict[str, Any] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token,
            cfg.signing_key,
            algorithms=[_JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "require": ["sub", "jti", "iat", "exp"],
            },
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationFailed() from exc
    sub = claims.get("sub")
    jti = claims.get("jti")
    if not isinstance(sub, str) or not sub or not isinstance(jti, str) or not jti:
        raise AuthenticationFailed()
    return claims


def issue_token_sync(user: Any) -> TokenProjection:
    """Trusted in-process issue: lock/replace the user's active token row."""
    if not getattr(user, "is_active", False):
        raise AuthenticationFailed()
    now = datetime.now(UTC)
    cfg = validate_jwt_settings()
    expires_at = now + cfg.token_ttl
    jti = secrets.token_urlsafe(32)
    digest = _digest(jti)
    user_pk = str(user.pk)
    with transaction.atomic():
        User = _user_model()
        locked = User.objects.select_for_update().filter(pk=user.pk).first()
        if locked is None or not locked.is_active:
            raise AuthenticationFailed()
        ApiToken.objects.update_or_create(
            user_id=user.pk,
            defaults={
                "jti_digest": digest,
                "issued_at": now,
                "expires_at": expires_at,
            },
        )
    token = _encode_token(user_pk=user_pk, jti=jti, issued_at=now, expires_at=expires_at)
    return TokenProjection(token=token, expires_at=expires_at)


async def issue_token(user: Any) -> TokenProjection:
    return await sync_to_async(issue_token_sync, thread_sensitive=True)(user)


def authenticate_bearer_sync(authorization_header: str | None) -> Any:
    """Validate ``Authorization: Bearer <token>`` and return the active user."""
    if authorization_header is None or not authorization_header:
        raise AuthenticationFailed()
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthenticationFailed()
    raw_token = parts[1].strip()
    claims = _decode_unverified_claims(raw_token)
    sub = str(claims["sub"])
    jti = str(claims["jti"])
    digest = _digest(jti)

    User = _user_model()
    user = User.objects.filter(pk=sub).first()
    if user is None or not user.is_active:
        raise AuthenticationFailed()

    row = ApiToken.objects.filter(user_id=user.pk).first()
    if row is None:
        raise AuthenticationFailed()
    if not hmac.compare_digest(row.jti_digest, digest):
        raise AuthenticationFailed()
    if row.expires_at < datetime.now(UTC):
        raise AuthenticationFailed()
    return user


async def authenticate_bearer(authorization_header: str | None) -> Any:
    return await sync_to_async(authenticate_bearer_sync, thread_sensitive=True)(
        authorization_header
    )


def rotate_token_sync(authorization_header: str | None) -> TokenProjection:
    """Atomically replace the presented active token. Losers get AuthenticationFailed."""
    if authorization_header is None or not authorization_header:
        raise AuthenticationFailed()
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthenticationFailed()
    raw_token = parts[1].strip()
    claims = _decode_unverified_claims(raw_token)
    sub = str(claims["sub"])
    jti = str(claims["jti"])
    presented_digest = _digest(jti)

    now = datetime.now(UTC)
    cfg = validate_jwt_settings()
    expires_at = now + cfg.token_ttl
    new_jti = secrets.token_urlsafe(32)
    new_digest = _digest(new_jti)

    with transaction.atomic():
        User = _user_model()
        user = User.objects.select_for_update().filter(pk=sub).first()
        if user is None or not user.is_active:
            raise AuthenticationFailed()
        # Conditional update: only the request holding the current digest wins.
        updated = ApiToken.objects.filter(
            user_id=user.pk,
            jti_digest=presented_digest,
        ).update(
            jti_digest=new_digest,
            issued_at=now,
            expires_at=expires_at,
        )
        if updated != 1:
            raise AuthenticationFailed()

    token = _encode_token(user_pk=str(user.pk), jti=new_jti, issued_at=now, expires_at=expires_at)
    return TokenProjection(token=token, expires_at=expires_at)


async def rotate_token(authorization_header: str | None) -> TokenProjection:
    return await sync_to_async(rotate_token_sync, thread_sensitive=True)(authorization_header)


def revoke_token_sync(authorization_header: str | None) -> None:
    """Conditionally delete the token row matching the current digest."""
    if authorization_header is None or not authorization_header:
        raise AuthenticationFailed()
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthenticationFailed()
    raw_token = parts[1].strip()
    claims = _decode_unverified_claims(raw_token)
    sub = str(claims["sub"])
    jti = str(claims["jti"])
    digest = _digest(jti)
    with transaction.atomic():
        deleted, _ = ApiToken.objects.filter(user_id=sub, jti_digest=digest).delete()
        if deleted == 0:
            raise AuthenticationFailed()


async def revoke_token(authorization_header: str | None) -> None:
    await sync_to_async(revoke_token_sync, thread_sensitive=True)(authorization_header)
