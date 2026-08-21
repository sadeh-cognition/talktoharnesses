"""Claude executable identity probing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.claude.probe import probe_claude


def _sdk(version: str = "0.1.53") -> SimpleNamespace:
    class _Options:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class _Client:
        def __init__(self, _options: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get_server_info(self) -> dict[str, object]:
            return {
                "models": [
                    {"value": "default", "displayName": "Default (recommended)"},
                    {"value": "sonnet", "displayName": "Sonnet"},
                ]
            }

    return SimpleNamespace(
        __version__=version,
        ClaudeAgentOptions=_Options,
        ClaudeSDKClient=_Client,
    )


@pytest.mark.asyncio
async def test_bundled_cli_probe_success_and_import_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktoharnesses.providers.claude import probe as probe_mod

    monkeypatch.setattr(
        probe_mod,
        "_import_claude_sdk",
        _sdk,
    )
    monkeypatch.setattr(probe_mod, "_bundled_cli_version", lambda: "2.1.88")
    caps, release = await probe_claude(
        HarnessConfiguration(
            kind=HarnessKind.CLAUDE,
            model="sonnet",
            working_directory="/tmp",
        )
    )
    assert release.cli_source == "bundled"
    assert caps.kind is HarnessKind.CLAUDE
    assert [(model.id, model.label) for model in caps.models] == [
        ("default", "Default (recommended)"),
        ("sonnet", "Sonnet"),
    ]

    def _import_fail() -> object:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "claude-agent-sdk extra is not installed",
            details={"extra": "claude"},
        )

    monkeypatch.setattr(probe_mod, "_import_claude_sdk", _import_fail)
    with pytest.raises(DomainError) as missing:
        await probe_claude(HarnessConfiguration(kind=HarnessKind.CLAUDE, working_directory="/tmp"))
    assert missing.value.code is ErrorCode.PROVIDER_INCOMPATIBLE

    def _bundled_fail() -> str:
        raise DomainError(
            ErrorCode.PROVIDER_INCOMPATIBLE,
            "unable to determine bundled Claude Code CLI version",
        )

    monkeypatch.setattr(
        probe_mod,
        "_import_claude_sdk",
        lambda: SimpleNamespace(__version__="0.1.53"),
    )
    monkeypatch.setattr(probe_mod, "_bundled_cli_version", _bundled_fail)
    with pytest.raises(DomainError):
        await probe_claude(HarnessConfiguration(kind=HarnessKind.CLAUDE, working_directory="/tmp"))

    monkeypatch.setattr(
        probe_mod,
        "_import_claude_sdk",
        lambda: SimpleNamespace(__version__=""),
    )
    with pytest.raises(DomainError) as no_ver:
        await probe_claude(HarnessConfiguration(kind=HarnessKind.CLAUDE, working_directory="/tmp"))
    assert no_ver.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
