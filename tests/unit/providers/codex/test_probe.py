"""Codex probe identity matching."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.codex import probe as probe_mod


class _Config:
    def __init__(self, *, cwd: str) -> None:
        self.cwd = cwd


def _sdk_with_response(data: list[object]) -> SimpleNamespace:
    class _Client:
        def __init__(self, _config: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def models(self) -> SimpleNamespace:
            return SimpleNamespace(data=data)

    return SimpleNamespace(__version__="0.144.4", AsyncCodex=_Client, CodexConfig=_Config)


def _model(
    model: str,
    *,
    display_name: str | None = None,
    efforts: tuple[str, ...] = (),
    is_default: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        display_name=display_name,
        supported_reasoning_efforts=[
            SimpleNamespace(reasoning_effort=SimpleNamespace(value=effort))
            for effort in efforts
        ],
        is_default=is_default,
    )


@pytest.mark.asyncio
async def test_probe_matches_pinned_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe_mod,
        "_import_openai_codex",
        lambda: _sdk_with_response(
            [
                _model(
                    "gpt-5.6-sol",
                    display_name="GPT-5.6-Sol",
                    efforts=("low", "medium", "high"),
                    is_default=True,
                )
            ]
        ),
    )
    monkeypatch.setattr(probe_mod, "_runtime_version", lambda: "0.144.4")
    caps, release = await probe_mod.probe_codex(
        HarnessConfiguration(kind=HarnessKind.CODEX, working_directory="/tmp")
    )
    assert release.id == "codex-openai-codex-0.144.4"
    assert caps.supports_resume is True
    assert [(model.id, model.label) for model in caps.models] == [("gpt-5.6-sol", "GPT-5.6-Sol")]
    assert [effort.id for effort in caps.efforts] == ["low", "medium", "high"]
    assert [effort.id for effort in caps.models[0].efforts or ()] == [
        "low",
        "medium",
        "high",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ([], [SimpleNamespace(model=None, display_name="bad")]))
async def test_codex_model_discovery_rejects_invalid_catalog(data: list[object]) -> None:
    with pytest.raises(DomainError) as exc:
        await probe_mod._discover_models(  # pyright: ignore[reportPrivateUsage]
            _sdk_with_response(data), "/tmp"
        )
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


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
