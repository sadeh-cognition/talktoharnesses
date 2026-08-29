"""Minimal Django settings for the split service — no DB, no contrib apps."""

import os

# Never used for cookies or sessions; the service is stateless HTTP + SSE.
SECRET_KEY = os.environ.get("TTH_SPLIT_SECRET_KEY", "insecure-split-service-key")

DEBUG = os.environ.get("TTH_SPLIT_DEBUG", "").lower() in {"1", "true", "yes"}

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS: list[str] = []

MIDDLEWARE: list[str] = []

ROOT_URLCONF = "tth_grok.urls"

# No ORM: sessions live in process memory; the proxy owns all persistence.
DATABASES: dict[str, dict[str, str]] = {}

USE_TZ = True
