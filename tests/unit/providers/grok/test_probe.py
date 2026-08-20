"""Grok --version probe with monkeypatched subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contract.fakes import _FakeAcpProcess  # pyright: ignore[reportPrivateUsage]

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    LaunchSnapshot,
)
from talktoharnesses.providers.grok import probe as probe_mod
from talktoharnesses.providers.grok.compatibility import match_release


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
        return _Proc(b"grok 1.0.1 (3cd0d0cbce)")

    async def models(*_a: object, **_k: object) -> str:
        return "Default model: grok-4.5\n\nAvailable models:\n  * grok-4.5 (default)\n"

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    monkeypatch.setattr(probe_mod, "run_model_command", models)

    async def no_resume(*_args: object) -> bool:
        return False

    monkeypatch.setattr(probe_mod, "_probe_load_session", no_resume)
    caps, release = await probe_mod.probe_grok(
        HarnessConfiguration(
            kind=HarnessKind.GROK,
            executable_path=str(executable),
            working_directory="/tmp",
        )
    )
    assert release.cli_version == "1.0.1"
    assert caps.kind is HarnessKind.GROK
    assert [model.id for model in caps.models] == ["grok-4.5"]
    assert caps.supports_resume is False

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


@pytest.mark.asyncio
async def test_grok_resume_probe_reads_initialize_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeAcpProcess(load_session=False)
    capabilities = HarnessCapabilities(
        kind=HarnessKind.GROK,
        version="1.0.0 (3cd0d0cbce) [stable]",
    )

    class _Supervisor:
        def build_launch_snapshot(self, **_kwargs: object) -> LaunchSnapshot:
            return LaunchSnapshot(
                resolved_executable="/tmp/grok",
                harness_version=capabilities.version,
                working_directory="/tmp",
                adapter_version="grok-capability-probe",
                capabilities=capabilities,
            )

        async def spawn(self, _spec: object) -> _FakeAcpProcess:
            return process

    monkeypatch.setattr(probe_mod, "ProcessSupervisor", _Supervisor)
    supports_resume = await probe_mod._probe_load_session(  # pyright: ignore[reportPrivateUsage]
        Path("/tmp/grok"),
        HarnessConfiguration(
            kind=HarnessKind.GROK,
            executable_path="/tmp/grok",
            working_directory="/tmp",
        ),
        match_release("grok 1.0.0 (3cd0d0cbce)", platform="linux"),
        capabilities,
    )

    assert supports_resume is False
    assert process.returncode == 0
