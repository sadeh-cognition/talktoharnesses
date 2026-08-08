"""Ninja bearer authentication using package JWT validation."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from ninja.security import HttpBearer

from talktoharnesses.django.auth import AuthenticationFailed, authenticate_bearer_sync


class BearerAuth(HttpBearer):
    """API-level HS256 bearer auth. Failures yield generic 401 via exception handlers."""

    def authenticate(self, request: HttpRequest, token: str) -> Any | None:
        try:
            return authenticate_bearer_sync(f"Bearer {token}")
        except AuthenticationFailed:
            return None
