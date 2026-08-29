"""Django management command entry for wiki_lint."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from tests.unit.test_wiki_lint import write_page


def test_wiki_lint_command_accepts_valid_vault(tmp_path: Path) -> None:
    write_page(tmp_path, "Home.md", title="Home")
    out = StringIO()

    call_command("wiki_lint", root=tmp_path, stdout=out)

    assert "Wiki lint passed." in out.getvalue()


def test_wiki_lint_command_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="wiki root is not a directory"):
        call_command("wiki_lint", root=tmp_path / "missing")
