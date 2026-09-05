"""Telemetry gate tests. The enabled happy path installs global providers and
is exercised via the proxy's sandbox smoke, not in-process here."""

from __future__ import annotations

import logging
import sys

import pytest

from tth_muse import telemetry


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


def test_service_name_matches_package() -> None:
    # Guards against copy-paste drift across the split repos.
    assert telemetry._SERVICE_NAME == "tth-muse"  # pyright: ignore[reportPrivateUsage]
