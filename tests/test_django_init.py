"""Django application initialization smoke tests."""

from __future__ import annotations

import django
from django.apps import apps
from django.conf import settings
from django.core.management import call_command


def _configure() -> None:
    if settings.configured:
        return
    settings.configure(
        DEBUG=True,
        SECRET_KEY="test-not-for-production",
        USE_TZ=True,
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "talktoharnesses.django",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    django.setup()


def test_django_setup_registers_app() -> None:
    _configure()
    # is_installed expects the full Python path, not the short label.
    assert apps.is_installed("talktoharnesses.django")
    config = apps.get_app_config("talktoharnesses")
    assert config.name == "talktoharnesses.django"
    assert config.label == "talktoharnesses"


def test_django_system_checks_pass() -> None:
    _configure()
    # Raises SystemCheckError on failure.
    call_command("check", verbosity=0)
