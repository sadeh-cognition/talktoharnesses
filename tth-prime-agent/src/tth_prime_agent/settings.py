"""Minimal Django settings for the split service — no DB, no contrib apps."""

import os

# Never used for cookies or sessions; the service is stateless HTTP + SSE.
SECRET_KEY = os.environ.get("TTH_SPLIT_SECRET_KEY", "insecure-split-service-key")

DEBUG = os.environ.get("TTH_SPLIT_DEBUG", "").lower() in {"1", "true", "yes"}

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS: list[str] = []

MIDDLEWARE: list[str] = []

ROOT_URLCONF = "tth_prime_agent.urls"

# No ORM: sessions live in process memory; the proxy owns all persistence.
DATABASES: dict[str, dict[str, str]] = {}

USE_TZ = True
# Django's default TIME_ZONE (America/Chicago) is applied to the process clock
# at setup, which stamped log lines five hours behind the UTC timestamps in
# agentbahn's records and the harness journals. Keep every log in UTC.
TIME_ZONE = "UTC"

# Split-service diagnostics go to the container's stdout (``docker logs``).
# ``TTH_SPLIT_LOG_LEVEL=DEBUG`` additionally traces every protocol frame's
# method. The ``tth_prime_agent`` logger only sets a level and propagates to the root
# handlers, so the OpenTelemetry ``LoggingHandler`` that ``telemetry.py``
# installs on the root logger receives the same records.
_LOG_LEVEL = os.environ.get("TTH_SPLIT_LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "split": {
            "()": "tth_prime_agent.shared.logs.UtcFormatter",
            "format": "%(asctime)sZ %(levelname)s %(name)s: %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        }
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "split",
        }
    },
    "root": {"handlers": ["stdout"], "level": "WARNING"},
    "loggers": {"tth_prime_agent": {"level": _LOG_LEVEL}},
}
