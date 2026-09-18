"""Split-private environment entries never reach harness children."""

from __future__ import annotations

import os

import pytest

from tth_cursor.shared import private_env


@pytest.fixture(autouse=True)
def _reset_sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(private_env, "_sealed", {})


def test_seal_moves_private_variables_out_of_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTH_SPLIT_TOKEN", "s3cret")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tth_cursor.settings")
    monkeypatch.setenv("XAI_API_KEY", "provider-key")

    private_env.seal()

    assert "TTH_SPLIT_TOKEN" not in os.environ
    assert "DJANGO_SETTINGS_MODULE" not in os.environ
    assert os.environ["XAI_API_KEY"] == "provider-key"
    assert private_env.split_token() == "s3cret"


def test_seal_twice_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTH_SPLIT_TOKEN", "s3cret")
    private_env.seal()

    private_env.seal()

    assert private_env.split_token() == "s3cret"
    assert "TTH_SPLIT_TOKEN" not in os.environ


def test_split_token_reads_the_live_environment_before_sealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TTH_SPLIT_TOKEN", raising=False)
    assert private_env.split_token() is None
    monkeypatch.setenv("TTH_SPLIT_TOKEN", "live")
    assert private_env.split_token() == "live"
