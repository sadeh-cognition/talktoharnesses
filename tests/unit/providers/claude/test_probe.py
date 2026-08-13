"""Claude executable identity probing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.claude.probe import probe_claude


class _VersionProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"2.1.88 (Claude Code)\n", b""


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
async def test_explicit_executable_requires_explicit_compatibility_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/tmp/claude-explicit")
    called: list[tuple[object, ...]] = []

    async def create_subprocess_exec(*argv: object, **kwargs: object) -> _VersionProcess:
        del kwargs
        called.append(argv)
        return _VersionProcess()

    monkeypatch.setattr(
        "talktoharnesses.providers.claude.probe._import_claude_sdk",
        lambda: SimpleNamespace(__version__="0.1.53"),
    )

    def resolve_executable(path: str) -> Path:
        del path
        return executable

    monkeypatch.setattr(
        "talktoharnesses.providers.claude.probe.resolve_executable",
        resolve_executable,
    )
    monkeypatch.setattr(
        "talktoharnesses.providers.claude.probe.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )
    config = HarnessConfiguration(
        kind=HarnessKind.CLAUDE,
        executable_path=str(executable),
        working_directory="/tmp",
    )
    with pytest.raises(DomainError) as exc_info:
        await probe_claude(config)
    assert exc_info.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert exc_info.value.details["cli_source"] == "explicit"
    assert called == [(str(executable), "--version")]


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


@pytest.mark.asyncio
async def test_explicit_version_oserror_and_bad_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktoharnesses.providers.claude import probe as probe_mod

    executable = Path("/tmp/claude-bad")
    monkeypatch.setattr(
        probe_mod,
        "_import_claude_sdk",
        lambda: SimpleNamespace(__version__="0.1.53"),
    )

    def _resolve_executable(_path: Path) -> Path:
        return executable

    monkeypatch.setattr(probe_mod, "resolve_executable", _resolve_executable)

    async def boom(*_a: object, **_k: object) -> _VersionProcess:
        raise OSError("nope")

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(DomainError) as os_exc:
        await probe_claude(
            HarnessConfiguration(
                kind=HarnessKind.CLAUDE,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert os_exc.value.code is ErrorCode.INVALID_EXECUTABLE

    class _BadRc(_VersionProcess):
        returncode = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"failed"

    async def bad_rc(*_a: object, **_k: object) -> _BadRc:
        return _BadRc()

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", bad_rc)
    with pytest.raises(DomainError) as rc_exc:
        await probe_claude(
            HarnessConfiguration(
                kind=HarnessKind.CLAUDE,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert rc_exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE

    class _Malformed(_VersionProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            return b"not-a-version\nwith-newline\n", b""

    async def malformed(*_a: object, **_k: object) -> _Malformed:
        return _Malformed()

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", malformed)
    with pytest.raises(DomainError) as malformed_exc:
        await probe_claude(
            HarnessConfiguration(
                kind=HarnessKind.CLAUDE,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert malformed_exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
