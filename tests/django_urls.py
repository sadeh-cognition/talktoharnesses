"""Test URLConf mounting the package API at ``/api/v1/``."""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("talktoharnesses.django.api.urls")),
]
