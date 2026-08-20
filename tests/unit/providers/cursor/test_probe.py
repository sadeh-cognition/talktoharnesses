"""Cursor agent --version probe with monkeypatched subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contract.fakes import _FakeAcpProcess  # pyright: ignore[reportPrivateUsage]

from talktoharnesses.domain.enums import ErrorCode, HarnessKind
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.models import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessEffortInfo,
    HarnessModelInfo,
    LaunchSnapshot,
)
from talktoharnesses.providers.cursor import probe as probe_mod
from talktoharnesses.providers.cursor.compatibility import match_release


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
        return (
            "Available models\n\nauto - Auto (default)\ncomposer-2.5 - Composer 2.5\n"
            "Tip: use --model <id> to switch.\n"
        )

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    monkeypatch.setattr(probe_mod, "run_model_command", models)

    async def efforts(
        _executable: Path,
        _config: HarnessConfiguration,
        _release: object,
        _capabilities: HarnessCapabilities,
        catalog: tuple[HarnessModelInfo, ...],
    ) -> tuple[tuple[HarnessEffortInfo, ...], tuple[HarnessModelInfo, ...], bool]:
        values = (
            HarnessEffortInfo(id="low", label="Low"),
            HarnessEffortInfo(id="medium", label="Medium"),
            HarnessEffortInfo(id="high", label="High"),
        )
        discovered = tuple(model.model_copy(update={"efforts": values}) for model in catalog)
        return values, discovered, True

    monkeypatch.setattr(probe_mod, "_discover_model_efforts", efforts)
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
    assert [effort.id for effort in caps.efforts] == ["low", "medium", "high"]

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


@pytest.mark.asyncio
async def test_cursor_effort_discovery_is_prompt_free_and_closes_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeAcpProcess(
        agent_name="cursor",
        agent_version="2026.08.04-aaa8809",
    )
    capabilities = HarnessCapabilities(
        kind=HarnessKind.CURSOR,
        version="2026.08.04-aaa8809",
    )

    class _Supervisor:
        def build_launch_snapshot(self, **_kwargs: object) -> LaunchSnapshot:
            return LaunchSnapshot(
                resolved_executable="/tmp/cursor-agent",
                harness_version="2026.08.04-aaa8809",
                working_directory="/tmp",
                adapter_version="cursor-effort-probe",
                capabilities=capabilities,
            )

        async def spawn(self, _spec: object) -> _FakeAcpProcess:
            return process

    monkeypatch.setattr(probe_mod, "ProcessSupervisor", _Supervisor)
    models = (
        HarnessModelInfo(id="auto", label="Auto"),
        HarnessModelInfo(id="composer-2.5", label="Composer 2.5"),
        HarnessModelInfo(id="gpt-5.6-sol", label="GPT-5.6 Sol"),
    )

    default_efforts, discovered, load_session = await probe_mod._discover_model_efforts(  # pyright: ignore[reportPrivateUsage]
        Path("/tmp/cursor-agent"),
        HarnessConfiguration(
            kind=HarnessKind.CURSOR,
            executable_path="/tmp/cursor-agent",
            working_directory="/tmp",
        ),
        match_release("2026.08.04-aaa8809", platform="linux"),
        capabilities,
        models,
    )

    assert default_efforts == ()
    assert load_session is True
    assert discovered[0].efforts == ()
    assert discovered[1].efforts == ()
    assert [effort.id for effort in discovered[2].efforts or ()] == [
        "low",
        "medium",
        "high",
    ]
    assert all(request.get("method") != "session/prompt" for request in process.requests)
    assert process.returncode == 0
