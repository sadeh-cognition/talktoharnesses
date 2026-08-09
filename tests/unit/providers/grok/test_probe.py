"""Grok --version probe with monkeypatched subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.grok import probe as probe_mod


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b"err"


@pytest.mark.asyncio
async def test_probe_grok_success_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    executable = Path("/tmp/grok")

    def _resolve_executable(_path: Path) -> Path:
        return executable

    monkeypatch.setattr(probe_mod, "resolve_executable", _resolve_executable)

    async def ok_exec(*_a: object, **_k: object) -> _Proc:
        return _Proc(b"grok 1.0.0 (3cd0d0cbce)")

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    caps, release = await probe_mod.probe_grok(
        HarnessConfiguration(
            kind=HarnessKind.GROK,
            executable_path=str(executable),
            working_directory="/tmp",
        )
    )
    assert release.cli_version == "1.0.0"
    assert caps.kind is HarnessKind.GROK

    with pytest.raises(DomainError) as no_path:
        await probe_mod.probe_grok(
            HarnessConfiguration(kind=HarnessKind.GROK, working_directory="/tmp")
        )
    assert no_path.value.code is ErrorCode.INVALID_EXECUTABLE

    async def boom(*_a: object, **_k: object) -> _Proc:
        raise OSError("cannot exec")

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(DomainError) as os_exc:
        await probe_mod.probe_grok(
            HarnessConfiguration(
                kind=HarnessKind.GROK,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert os_exc.value.code is ErrorCode.INVALID_EXECUTABLE

    async def bad_rc(*_a: object, **_k: object) -> _Proc:
        return _Proc(b"", returncode=3)

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", bad_rc)
    with pytest.raises(DomainError) as rc_exc:
        await probe_mod.probe_grok(
            HarnessConfiguration(
                kind=HarnessKind.GROK,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert rc_exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
