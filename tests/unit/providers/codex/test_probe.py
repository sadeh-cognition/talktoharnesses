"""Codex probe identity matching."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.codex import probe as probe_mod


@pytest.mark.asyncio
async def test_probe_matches_pinned_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe_mod,
        "_import_openai_codex",
        lambda: SimpleNamespace(__version__="0.144.4"),
    )
    monkeypatch.setattr(probe_mod, "_runtime_version", lambda: "0.144.4")
    caps, release = await probe_mod.probe_codex(
        HarnessConfiguration(kind=HarnessKind.CODEX, working_directory="/tmp")
    )
    assert release.id == "codex-openai-codex-0.144.4"
    assert caps.supports_resume is True


@pytest.mark.asyncio
async def test_probe_missing_version_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe_mod,
        "_import_openai_codex",
        lambda: SimpleNamespace(__version__=""),
    )
    with pytest.raises(DomainError) as exc:
        await probe_mod.probe_codex(
            HarnessConfiguration(kind=HarnessKind.CODEX, working_directory="/tmp")
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_import_and_runtime_version_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    monkeypatch.setattr(
        probe_mod,
        "_import_openai_codex",
        lambda: SimpleNamespace(__version__="0.144.4"),
    )
    assert probe_mod._import_openai_codex().__version__ == "0.144.4"  # pyright: ignore[reportPrivateUsage]

    def _missing_import() -> object:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "openai-codex extra is not installed",
            details={"extra": "codex"},
        )

    monkeypatch.setattr(probe_mod, "_import_openai_codex", _missing_import)
    with pytest.raises(DomainError):
        probe_mod._import_openai_codex()  # pyright: ignore[reportPrivateUsage]

    def version(name: str) -> str:
        if name == "openai-codex-cli-bin":
            raise importlib.metadata.PackageNotFoundError(name)
        return "0.144.4"

    monkeypatch.setattr(importlib.metadata, "version", version)
    # Restore real helper body for runtime version fallback.
    monkeypatch.undo()
    monkeypatch.setattr(importlib.metadata, "version", version)
    assert probe_mod._runtime_version() == "0.144.4"  # pyright: ignore[reportPrivateUsage]
