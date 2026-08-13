"""Shared model discovery subprocess behavior."""

from pathlib import Path

import pytest

from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.providers import _model_discovery as discovery


class _FailedProcess:
    returncode = 2

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b"catalog unavailable"


@pytest.mark.asyncio
async def test_model_command_maps_launch_and_command_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cannot_launch(*_args: object, **_kwargs: object) -> _FailedProcess:
        raise OSError("cannot exec")

    monkeypatch.setattr(discovery.asyncio, "create_subprocess_exec", cannot_launch)
    with pytest.raises(DomainError) as launch:
        await discovery.run_model_command(
            Path("/missing"),
            "models",
            provider="Example",
            working_directory="/tmp",
        )
    assert launch.value.code is ErrorCode.INVALID_EXECUTABLE

    async def failed(*_args: object, **_kwargs: object) -> _FailedProcess:
        return _FailedProcess()

    monkeypatch.setattr(discovery.asyncio, "create_subprocess_exec", failed)
    with pytest.raises(DomainError) as command:
        await discovery.run_model_command(
            Path("/example"),
            "models",
            provider="Example",
            working_directory="/tmp",
        )
    assert command.value.code is ErrorCode.PROVIDER_INCOMPATIBLE
    assert command.value.details["stderr"] == "catalog unavailable"


@pytest.mark.asyncio
async def test_model_command_preserves_missing_working_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(DomainError) as exc:
        await discovery.run_model_command(
            Path("/example"),
            "models",
            provider="Example",
            working_directory=str(tmp_path / "missing"),
        )
    assert exc.value.code is ErrorCode.WORKING_DIRECTORY_NOT_FOUND
