"""URLconf for host inclusion at ``/api/v1/``."""

from __future__ import annotations

from django.urls import path

from talktoharnesses.django.api import api

urlpatterns = [
    path("", api.urls),
]
