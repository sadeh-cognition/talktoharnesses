"""Ensure Django model state matches committed migrations."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_no_migration_drift() -> None:
    out = StringIO()
    # Raises SystemExit / CommandError when unapplied model changes exist.
    call_command("makemigrations", "talktoharnesses", check=True, dry_run=True, stdout=out)
