"""Workspace support-document aggregation contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_render_supported_write_and_check(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "SUPPORTED_HARNESSES.md"
    command = [sys.executable, str(root / "scripts" / "render_supported.py")]

    written = subprocess.run(
        [*command, "--output", str(output)], check=False, capture_output=True, text=True
    )
    assert written.returncode == 0, written.stdout + written.stderr
    rendered = output.read_text(encoding="utf-8")
    for title in ("Grok", "Cursor", "Codex", "Claude Code", "OpenCode", "Prime Agent"):
        assert f"## {title}" in rendered

    checked = subprocess.run(
        [*command, "--check", "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr

    output.write_text("stale\n", encoding="utf-8")
    stale = subprocess.run(
        [*command, "--check", "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stale.returncode == 1
