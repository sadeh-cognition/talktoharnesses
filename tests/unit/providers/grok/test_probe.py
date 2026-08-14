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


@pytest.mark.parametrize(
    "output",
    ("", "Available models:\nnot-a-row", "Available models:\n* bad model"),
)
def test_grok_model_list_rejects_malformed_output(output: str) -> None:
    with pytest.raises(DomainError) as exc:
        probe_mod._parse_models(output)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


def test_grok_model_list_accepts_1_0_3_output() -> None:
    models = probe_mod._parse_models(  # pyright: ignore[reportPrivateUsage]
        "Default model: grok-4.6\n\nAvailable models:\n  * grok-4.6 (default)\n  - grok-4.5\n"
    )

    assert [model.id for model in models] == ["grok-4.6", "grok-4.5"]


@pytest.mark.asyncio
async def test_probe_grok_success_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    executable = Path("/tmp/grok")

    def _resolve_executable(_path: Path) -> Path:
        return executable

    monkeypatch.setattr(probe_mod, "resolve_executable", _resolve_executable)

    async def ok_exec(*_a: object, **_k: object) -> _Proc:
        return _Proc(b"grok 1.0.0 (3cd0d0cbce)")

    async def models(*_a: object, **_k: object) -> str:
        return "Default model: grok-4.5\n\nAvailable models:\n  * grok-4.5 (default)\n"

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    monkeypatch.setattr(probe_mod, "run_model_command", models)
    caps, release = await probe_mod.probe_grok(
        HarnessConfiguration(
            kind=HarnessKind.GROK,
            executable_path=str(executable),
            working_directory="/tmp",
        )
    )
    assert release.cli_version == "1.0.0"
    assert caps.kind is HarnessKind.GROK
    assert [model.id for model in caps.models] == ["grok-4.5"]

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
