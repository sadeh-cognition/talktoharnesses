"""CLI coverage for render_supported entrypoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.providers import render_supported


def test_main_check_and_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "SUPPORTED.md"
    assert render_supported.main(["--output", str(out)]) == 0
    assert out.is_file()
    assert render_supported.main(["--check", "--output", str(out)]) == 0
    out.write_text("stale\n", encoding="utf-8")
    assert render_supported.main(["--check", "--output", str(out)]) == 1
    captured = capsys.readouterr()
    assert "out of date" in captured.out
    assert render_supported.main(["--check", "--output", str(tmp_path / "missing.md")]) == 1


def test_grok_compat_main_invocation() -> None:
    from talktoharnesses.providers.grok import render_supported as grok_rs

    assert callable(grok_rs.main)


def test_render_supported_module_main(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["render_supported", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("talktoharnesses.providers.render_supported", run_name="__main__")
    assert exc.value.code in {0, 2}


def test_grok_render_supported_module_main(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["render_supported", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("talktoharnesses.providers.grok.render_supported", run_name="__main__")
    assert exc.value.code in {0, 2}
