"""Host telemetry gate and wiring tests.

The enabled happy path runs in a subprocess: ``configure_opentelemetry``
installs global tracer/meter/logger providers, which other tests in this
session (observability, secret-scan) install for themselves.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest
from host import telemetry

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("false", True),
        ("FALSE", True),
        (" False ", True),
        ("0", True),
        ("00", False),
        ("no", False),
        ("http://collector:4318", False),
    ],
)
def test_telemetry_opted_out(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    else:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", value)
    assert telemetry.telemetry_opted_out() is expected


def test_configure_is_noop_when_opted_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "0")
    monkeypatch.setattr(telemetry, "_telemetry_enabled", False)
    root_handlers_before = list(logging.getLogger().handlers)

    telemetry.configure_opentelemetry()

    assert telemetry._telemetry_enabled is False  # pyright: ignore[reportPrivateUsage]
    assert list(logging.getLogger().handlers) == root_handlers_before


def test_configure_hard_fails_without_otel_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_telemetry_enabled", False)
    # A None entry in sys.modules makes the import raise ImportError before
    # any global provider is installed.
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)

    with pytest.raises(RuntimeError, match="opt out"):
        telemetry.configure_opentelemetry()

    assert telemetry._telemetry_enabled is False  # pyright: ignore[reportPrivateUsage]


def _run_asgi_import(tmp_path: Path, endpoint: str, check_script: str) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "TTH_LOG_FILE": str(tmp_path / "host.log"),
        "TTH_DB_PATH": str(tmp_path / "db.sqlite3"),
        # Batch processors flush on exit; keep the doomed export attempt short.
        "OTEL_EXPORTER_OTLP_TIMEOUT": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", check_script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_asgi_import_enables_telemetry_and_keeps_log_sinks(tmp_path: Path) -> None:
    # Regression test for the startup ordering bug: configure_logging()'s
    # logger.remove() must not strip the OTel loguru sink.
    script = (
        "import host.asgi\n"
        "import host.telemetry\n"
        "assert host.telemetry._telemetry_enabled\n"
        "from loguru import logger\n"
        "sinks = [repr(h) for h in logger._core.handlers.values()]\n"
        "assert any('LoggingHandler' in s for s in sinks), sinks\n"
        "import logging\n"
        "names = [type(h).__name__ for h in logging.getLogger().handlers]\n"
        "assert 'LoggingHandler' in names, names\n"
        "import os; os._exit(0)\n"
    )
    _run_asgi_import(tmp_path, "http://127.0.0.1:1", script)


def test_asgi_import_respects_opt_out(tmp_path: Path) -> None:
    script = (
        "import host.asgi\n"
        "import host.telemetry\n"
        "assert not host.telemetry._telemetry_enabled\n"
        "import logging\n"
        "names = [type(h).__name__ for h in logging.getLogger().handlers]\n"
        "assert 'LoggingHandler' not in names, names\n"
    )
    _run_asgi_import(tmp_path, "0", script)
