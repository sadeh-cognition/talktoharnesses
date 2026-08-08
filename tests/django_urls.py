"""Test URLConf mounting the package API at ``/api/v1/``."""

from __future__ import annotations

from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("talktoharnesses.django.api.urls")),
]
