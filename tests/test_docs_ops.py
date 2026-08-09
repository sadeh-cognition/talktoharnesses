"""Executable documentation and operator-doc hygiene checks."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = [
    ROOT / "README.md",
    ROOT / "docs" / "deployment.md",
    ROOT / "docs" / "upgrading.md",
    ROOT / "docs" / "live-testing.md",
    ROOT / "docs" / "releasing.md",
    ROOT / "docs" / "performance.md",
    ROOT / "docs" / "search-retention-transcripts.md",
]


def test_operator_docs_exist() -> None:
    for path in DOC_PATHS:
        assert path.is_file(), path


def test_readme_django_setup_snippet_executes(tmp_path: Path) -> None:
    """Execute the documented Django settings/ASGI/URL composition in a host project."""
    host = tmp_path / "host"
    host.mkdir()
    (host / "settings.py").write_text(
        """
SECRET_KEY = "host-secret-key-not-for-production-use"
TALKTOHARNESSES_JWT_SIGNING_KEY = "replace-with-a-secret-at-least-32-bytes"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "talktoharnesses.django",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ROOT_URLCONF = "urls"
USE_TZ = True
""",
        encoding="utf-8",
    )
    (host / "urls.py").write_text(
        """
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("talktoharnesses.django.api.urls")),
]
""",
        encoding="utf-8",
    )
    (host / "asgi.py").write_text(
        """
from django.core.asgi import get_asgi_application
from talktoharnesses.django.asgi import talktoharnesses_lifespan

application = talktoharnesses_lifespan(get_asgi_application())
""",
        encoding="utf-8",
    )
    code = """
import django
from django.core.checks import run_checks
from django.core.management import call_command

django.setup()
assert run_checks() == []
call_command("makemigrations", "talktoharnesses", "--check", "--dry-run", verbosity=0)
import asgi
assert callable(asgi.application)
from talktoharnesses.django.auth import issue_token
assert callable(issue_token)
"""
    env = __import__("os").environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "settings"
    env["PYTHONPATH"] = str(host)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=host,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_and_workflows_have_no_credential_like_material() -> None:
    suspicious = re.compile(
        r"(?i)(sk-[A-Za-z0-9]{20,}|api[_-]?key\s*=\s*['\"][^'\"]+['\"]|"
        r"-----BEGIN (RSA |OPENSSH )?PRIVATE KEY-----|"
        r"/home/[A-Za-z0-9._-]+/(?:\.ssh|secrets))"
    )
    paths = list(DOC_PATHS)
    paths.extend(ROOT.glob(".github/workflows/*.yml"))
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        match = suspicious.search(text)
        assert match is None, f"{path}: {match.group(0) if match else ''}"


@pytest.mark.django_db
def test_documented_cleanup_command_is_importable() -> None:
    from django.core.management import load_command_class

    command = load_command_class("talktoharnesses.django", "talktoharnesses_cleanup")
    assert command is not None
