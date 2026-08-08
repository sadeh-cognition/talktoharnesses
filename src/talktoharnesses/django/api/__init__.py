"""Versioned Django-Ninja API at ``/api/v1``."""

from __future__ import annotations

from ninja import NinjaAPI

from talktoharnesses.django.api.auth import BearerAuth
from talktoharnesses.django.api.errors import register_exception_handlers
from talktoharnesses.django.api.routes import router

api = NinjaAPI(
    title="talktoharnesses",
    version="1.0.0",
    urls_namespace="talktoharnesses_api",
    auth=BearerAuth(),
    docs_url="/docs",
    openapi_url="/openapi.json",
)
register_exception_handlers(api)
api.add_router("", router)

__all__ = ["api"]
