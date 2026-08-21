"""Cursor agent --version probe with monkeypatched subprocess."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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


def _resolve_to(executable: Path) -> Callable[[HarnessKind], Path]:
    def resolve(_kind: HarnessKind) -> Path:
        return executable

    return resolve


@pytest.fixture(autouse=True)
def clear_effort_discovery_state() -> None:
    probe_mod._EFFORT_CACHE.clear()  # pyright: ignore[reportPrivateUsage]
    probe_mod._EFFORT_IN_FLIGHT.clear()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("output", ("", "Available models", "Available models\nbad-row"))
def test_cursor_model_list_rejects_malformed_output(output: str) -> None:
    with pytest.raises(DomainError) as exc:
        probe_mod._parse_models(output)  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


@pytest.mark.asyncio
async def test_probe_cursor_success_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    executable = Path("/tmp/cursor-agent")

    def _resolve_kind(_kind: object) -> Path:
        return executable

    monkeypatch.setattr(probe_mod, "resolve_kind_executable", _resolve_kind)

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

    async def boom(*_a: object, **_k: object) -> _Proc:
        raise OSError("cannot exec")

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(DomainError) as os_exc:
        await probe_mod.probe_cursor(
            HarnessConfiguration(
                kind=HarnessKind.CURSOR,
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
                working_directory="/tmp",
            )
        )
    assert rc_exc.value.code is ErrorCode.PROVIDER_INCOMPATIBLE


@pytest.mark.asyncio
async def test_cursor_effort_discovery_is_prompt_free_and_closes_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[_FakeAcpProcess] = []
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
            process = _FakeAcpProcess(
                agent_name="cursor",
                agent_version="2026.08.04-aaa8809",
            )
            processes.append(process)
            return process

    monkeypatch.setattr(probe_mod, "ProcessSupervisor", _Supervisor)
    monkeypatch.setattr(probe_mod, "_EFFORT_WORKER_COUNT", 2)
    models = (
        HarnessModelInfo(id="auto", label="Auto"),
        HarnessModelInfo(id="composer-2.5", label="Composer 2.5"),
        HarnessModelInfo(id="gpt-5.6-sol", label="GPT-5.6 Sol"),
    )

    default_efforts, discovered, load_session = await probe_mod._discover_model_efforts(  # pyright: ignore[reportPrivateUsage]
        Path("/tmp/cursor-agent"),
        HarnessConfiguration(
            kind=HarnessKind.CURSOR,
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
    assert len(processes) == 2
    requests = [request for process in processes for request in process.requests]
    assert all(request.get("method") != "session/prompt" for request in requests)
    selected = sorted(
        str(request["params"]["value"])
        for request in requests
        if request.get("method") == "session/set_config_option"
    )
    assert selected == ["composer-2.5", "gpt-5.6-sol"]
    assert all(process.returncode == 0 for process in processes)


@pytest.mark.asyncio
async def test_cursor_effort_discovery_rejects_inconsistent_worker_catalogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[_FakeAcpProcess] = []
    capabilities = HarnessCapabilities(
        kind=HarnessKind.CURSOR,
        version="2026.08.04-aaa8809",
    )

    class _DifferentCatalogProcess(_FakeAcpProcess):
        def _cursor_config_options(self) -> list[dict[str, object]]:  # pyright: ignore[reportPrivateUsage]
            options = super()._cursor_config_options()  # pyright: ignore[reportPrivateUsage]
            model = options[0]
            values = model["options"]
            assert isinstance(values, list)
            model["options"] = values[:-1]
            return options

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
            process = (
                _FakeAcpProcess(
                    agent_name="cursor",
                    agent_version="2026.08.04-aaa8809",
                )
                if not processes
                else _DifferentCatalogProcess(
                    agent_name="cursor",
                    agent_version="2026.08.04-aaa8809",
                )
            )
            processes.append(process)
            return process

    monkeypatch.setattr(probe_mod, "ProcessSupervisor", _Supervisor)
    monkeypatch.setattr(probe_mod, "_EFFORT_WORKER_COUNT", 2)

    with pytest.raises(DomainError, match="inconsistent model options"):
        await probe_mod._discover_model_efforts(  # pyright: ignore[reportPrivateUsage]
            Path("/tmp/cursor-agent"),
            HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp"),
            match_release("2026.08.04-aaa8809", platform="linux"),
            capabilities,
            (
                HarnessModelInfo(id="auto", label="Auto"),
                HarnessModelInfo(id="composer-2.5", label="Composer 2.5"),
            ),
        )

    assert len(processes) == 2
    assert all(process.returncode == 0 for process in processes)


@pytest.mark.asyncio
async def test_cursor_effort_discovery_closes_open_workers_when_another_fails_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[_FakeAcpProcess] = []
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
            process = _FakeAcpProcess(
                agent_name="cursor",
                agent_version="2026.08.04-aaa8809",
            )
            processes.append(process)
            return process

    original_open = probe_mod._open_effort_worker  # pyright: ignore[reportPrivateUsage]
    first_opened = asyncio.Event()
    open_calls = 0

    async def open_worker(*args: object) -> object:
        nonlocal open_calls
        index = open_calls
        open_calls += 1
        if index == 1:
            await first_opened.wait()
            raise OSError("spawn failed")
        worker = await original_open(*args)  # type: ignore[arg-type]
        first_opened.set()
        return worker

    monkeypatch.setattr(probe_mod, "ProcessSupervisor", _Supervisor)
    monkeypatch.setattr(probe_mod, "_open_effort_worker", open_worker)
    monkeypatch.setattr(probe_mod, "_EFFORT_WORKER_COUNT", 2)

    with pytest.raises(OSError, match="spawn failed"):
        await probe_mod._discover_model_efforts(  # pyright: ignore[reportPrivateUsage]
            Path("/tmp/cursor-agent"),
            HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp"),
            match_release("2026.08.04-aaa8809", platform="linux"),
            capabilities,
            (
                HarnessModelInfo(id="auto", label="Auto"),
                HarnessModelInfo(id="composer-2.5", label="Composer 2.5"),
            ),
        )

    assert len(processes) == 1
    assert processes[0].returncode == 0


@pytest.mark.asyncio
async def test_cursor_effort_discovery_is_single_flight_and_survives_waiter_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/tmp/cursor-agent")
    monkeypatch.setattr(probe_mod, "resolve_kind_executable", _resolve_to(executable))

    async def ok_exec(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(b"2026.08.04-aaa8809")

    output = "Available models\n\nauto - Auto (default)\n"

    async def list_models(*_args: object, **_kwargs: object) -> str:
        return output

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    monkeypatch.setattr(probe_mod, "run_model_command", list_models)

    started = asyncio.Event()
    release_discovery = asyncio.Event()
    calls: list[str] = []
    result = (
        (),
        (HarnessModelInfo(id="auto", label="Auto", efforts=()),),
        True,
    )

    async def discover(
        _executable: Path,
        config: HarnessConfiguration,
        _release: object,
        _capabilities: HarnessCapabilities,
        _models: tuple[HarnessModelInfo, ...],
    ) -> tuple[tuple[HarnessEffortInfo, ...], tuple[HarnessModelInfo, ...], bool]:
        calls.append(config.working_directory)
        started.set()
        await release_discovery.wait()
        return result

    monkeypatch.setattr(probe_mod, "_discover_model_efforts", discover)
    first = asyncio.create_task(
        probe_mod.probe_cursor(
            HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp/shared")
        )
    )
    await started.wait()
    second = asyncio.create_task(
        probe_mod.probe_cursor(
            HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp/shared")
        )
    )
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release_discovery.set()
    second_capabilities, _release = await second
    third_capabilities, _release = await probe_mod.probe_cursor(
        HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp/shared")
    )
    workspace_capabilities, _release = await probe_mod.probe_cursor(
        HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp/other")
    )

    assert calls == ["/tmp/shared", "/tmp/other"]
    assert second_capabilities.models == result[1]
    assert third_capabilities.models == result[1]
    assert workspace_capabilities.models == result[1]


@pytest.mark.asyncio
async def test_failed_cursor_effort_discovery_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("/tmp/cursor-agent")
    monkeypatch.setattr(probe_mod, "resolve_kind_executable", _resolve_to(executable))

    async def ok_exec(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(b"2026.08.04-aaa8809")

    async def list_models(*_args: object, **_kwargs: object) -> str:
        return "Available models\n\nauto - Auto (default)\n"

    monkeypatch.setattr(probe_mod.asyncio, "create_subprocess_exec", ok_exec)
    monkeypatch.setattr(probe_mod, "run_model_command", list_models)
    attempts = 0

    async def discover(
        _executable: Path,
        _config: HarnessConfiguration,
        _release: object,
        _capabilities: HarnessCapabilities,
        models: tuple[HarnessModelInfo, ...],
    ) -> tuple[tuple[HarnessEffortInfo, ...], tuple[HarnessModelInfo, ...], bool]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DomainError(ErrorCode.PROVIDER_INCOMPATIBLE, "temporary failure")
        return (), models, True

    monkeypatch.setattr(probe_mod, "_discover_model_efforts", discover)
    config = HarnessConfiguration(kind=HarnessKind.CURSOR, working_directory="/tmp")

    with pytest.raises(DomainError, match="temporary failure"):
        await probe_mod.probe_cursor(config)
    capabilities, _release = await probe_mod.probe_cursor(config)

    assert attempts == 2
    assert [model.id for model in capabilities.models] == ["auto"]
