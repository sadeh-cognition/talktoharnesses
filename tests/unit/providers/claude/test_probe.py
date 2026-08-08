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
