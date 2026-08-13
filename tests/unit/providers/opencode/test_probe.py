"""OpenCode --version probe with monkeypatched subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.opencode import probe as probe_mod


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b"err"


@pytest.mark.parametrize("output", ("", "not-a-provider-model", "bad model/id"))
def test_opencode_model_list_rejects_malformed_output(output: str) -> None:
    with pytest.raises(DomainError) as exc:
        probe_mod._parse_models(output)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


@pytest.mark.asyncio
async def test_probe_opencode_success_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    executable = Path("/tmp/opencode")

    def _resolve_executable(_path: Path) -> Path:
        return executable

    monkeypatch.setattr(probe_mod, "resolve_executable", _resolve_executable)

    async def ok_exec(*_a: object, **_k: object) -> _Proc:
        return _Proc(b"1.2.27")

    async def models(*_a: object, **_k: object) -> str:
        return "opencode/big-pickle\nlmstudio/openai/gpt-oss-20b\n"

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    monkeypatch.setattr(probe_mod, "run_model_command", models)
    caps, release = await probe_mod.probe_opencode(
        HarnessConfiguration(
            kind=HarnessKind.OPENCODE,
            executable_path=str(executable),
            model="configured/typo",
            working_directory="/tmp",
        )
    )
    assert release.cli_version == "1.2.27"
    assert caps.kind is HarnessKind.OPENCODE
    assert [model.id for model in caps.models] == [
        "opencode/big-pickle",
        "lmstudio/openai/gpt-oss-20b",
    ]

    with pytest.raises(DomainError) as no_path:
        await probe_mod.probe_opencode(
            HarnessConfiguration(kind=HarnessKind.OPENCODE, working_directory="/tmp")
        )
    assert no_path.value.code is ErrorCode.INVALID_EXECUTABLE

    async def boom(*_a: object, **_k: object) -> _Proc:
        raise OSError("cannot exec")

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(DomainError) as os_exc:
        await probe_mod.probe_opencode(
            HarnessConfiguration(
                kind=HarnessKind.OPENCODE,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert os_exc.value.code is ErrorCode.INVALID_EXECUTABLE

    async def bad_rc(*_a: object, **_k: object) -> _Proc:
        return _Proc(b"", returncode=1)

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", bad_rc)
    with pytest.raises(DomainError) as rc_exc:
        await probe_mod.probe_opencode(
            HarnessConfiguration(
                kind=HarnessKind.OPENCODE,
                executable_path=str(executable),
                working_directory="/tmp",
            )
        )
    assert rc_exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
