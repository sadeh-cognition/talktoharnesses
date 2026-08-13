"""Cursor agent --version probe with monkeypatched subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.cursor import probe as probe_mod


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b"err"


@pytest.mark.parametrize("output", ("", "Available models", "Available models\nbad-row"))
def test_cursor_model_list_rejects_malformed_output(output: str) -> None:
    with pytest.raises(DomainError) as exc:
        probe_mod._parse_models(output)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


@pytest.mark.asyncio
async def test_probe_cursor_success_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    executable = Path("/tmp/cursor-agent")

    def _resolve_executable(_path: Path) -> Path:
        return executable

    monkeypatch.setattr(probe_mod, "resolve_executable", _resolve_executable)

    async def ok_exec(*_a: object, **_k: object) -> _Proc:
        return _Proc(b"2026.08.04-aaa8809")

    async def models(*_a: object, **_k: object) -> str:
        return "Available models\n\nauto - Auto (default)\ncomposer-2.5 - Composer 2.5\n"

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    monkeypatch.setattr(probe_mod, "run_model_command", models)
    caps, release = await probe_mod.probe_cursor(
        HarnessConfiguration(
            kind=HarnessKind.CURSOR,
            executable_path=str(executable),
            working_directory="/tmp",
        )
    )
    assert release.cli_version == "2026.08.04-aaa8809"
    assert caps.kind is HarnessKind.CURSOR
    assert [(model.id, model.label) for model in caps.models] == [
        ("auto", "Auto"),
        ("composer-2.5", "Composer 2.5"),
    ]

    with pytest.raises(DomainError) as no_path:
        await probe_mod.probe_cursor(
            HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp")
        )
    assert no_path.value.code is ErrorCode.INVALID_EXECUTABLE

    async def boom(*_a: object, **_k: object) -> _Proc:
        raise OSError("cannot exec")

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(DomainError) as os_exc:
        await probe_mod.probe_cursor(
            HarnessConfiguration(
                kind=HarnessKind.CURSOR,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert os_exc.value.code is ErrorCode.INVALID_EXECUTABLE

    async def bad_rc(*_a: object, **_k: object) -> _Proc:
        return _Proc(b"", returncode=2)

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", bad_rc)
    with pytest.raises(DomainError) as rc_exc:
        await probe_mod.probe_cursor(
            HarnessConfiguration(
                kind=HarnessKind.CURSOR,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert rc_exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
