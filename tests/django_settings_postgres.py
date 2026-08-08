"""PostgreSQL Django settings for CI / opt-in local runs.

Selected when ``TALKTOHARNESSES_TEST_DATABASE=postgres`` (or DATABASE_URL).
"""

from __future__ import annotations

import os

from tests.django_settings import *  # noqa: F403

_db_url = os.environ.get("DATABASE_URL", "")
if _db_url.startswith("postgres"):
    # DATABASE_URL=postgres://user:pass@host:port/db
    import urllib.parse as _url

    parsed = _url.urlparse(_db_url)
    _databases = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/") or "talktoharnesses",
            "USER": parsed.username or "postgres",
            "PASSWORD": parsed.password or "postgres",
            "HOST": parsed.hostname or "127.0.0.1",
            "PORT": str(parsed.port or 5432),
        }
    }
else:
    _databases = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "talktoharnesses"),
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }

DATABASES.update(_databases)  # noqa: F405
