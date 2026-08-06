"""ACP model selection (#4) and workspace confinement (#15)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from talktoharnesses.acp.runtime import (
    AcpRuntime,
    AcpSpawnInput,
    _HarnessAcpClient,
    resolve_model_config_update,
)


@dataclass
class Choice:
    value: str
    name: str | None = None


@dataclass
class SelectOption:
    id: str
    name: str
    options: list[Choice] = field(default_factory=list)


def test_resolve_matches_option_value() -> None:
    options = [SelectOption(id="model", name="Model", options=[Choice("gpt-5"), Choice("opus")])]
    assert resolve_model_config_update(options, "opus") == ("model", "opus")


def test_resolve_matches_display_name() -> None:
    options = [SelectOption(id="modelId", name="Model", options=[Choice("o-1", "Opus 5")])]
    assert resolve_model_config_update(options, "Opus 5") == ("modelId", "o-1")


def test_resolve_ignores_unrelated_selects() -> None:
    options = [SelectOption(id="theme", name="Theme", options=[Choice("dark")])]
    assert resolve_model_config_update(options, "dark") is None


def test_resolve_returns_none_when_model_absent() -> None:
    options = [SelectOption(id="model", name="Model", options=[Choice("gpt-5")])]
    assert resolve_model_config_update(options, "nope") is None


def test_resolve_accepts_dict_shaped_options() -> None:
    options = [{"id": "model", "name": "Model", "options": [{"value": "grok-build"}]}]
    assert resolve_model_config_update(options, "grok-build") == ("model", "grok-build")


# --- applying the selection ------------------------------------------------


class FakeConn:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.fail = fail

    async def set_config_option(self, config_id: str, session_id: str, value: Any) -> Any:
        if self.fail:
            raise RuntimeError("agent rejected the option")
        self.calls.append((config_id, session_id, value))
        return None


@dataclass
class FakeSessionResponse:
    config_options: list[Any]


def _runtime(tmp_path: Path) -> AcpRuntime:
    return AcpRuntime(AcpSpawnInput(command=["true"], cwd=tmp_path, provider="cursor"))


async def test_applied_model_is_reported(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt._conn = FakeConn()
    resp = FakeSessionResponse(
        config_options=[SelectOption(id="model", name="Model", options=[Choice("opus")])]
    )
    applied = await rt._apply_model("s1", resp, "opus")
    assert applied == "opus"
    assert rt._conn.calls == [("model", "s1", "opus")]


async def test_unmatched_model_warns_and_reports_nothing(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt._conn = FakeConn()
    q = rt._bus.subscribe()
    resp = FakeSessionResponse(config_options=[])

    applied = await rt._apply_model("s1", resp, "does-not-exist")

    assert applied is None, "must not claim a model that is not in effect"
    warning = q.get_nowait()
    assert warning is not None
    assert warning.type == "runtime.warning"
    assert warning.code == "model_not_applied"


async def test_rejected_model_warns_and_reports_nothing(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt._conn = FakeConn(fail=True)
    q = rt._bus.subscribe()
    resp = FakeSessionResponse(
        config_options=[SelectOption(id="model", name="Model", options=[Choice("opus")])]
    )

    applied = await rt._apply_model("s1", resp, "opus")

    assert applied is None
    warning = q.get_nowait()
    assert warning is not None
    assert warning.code == "model_not_applied"


async def test_no_model_requested_is_silent(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    rt._conn = FakeConn()
    q = rt._bus.subscribe()
    assert await rt._apply_model("s1", FakeSessionResponse([]), None) is None
    assert q.empty()


# --- workspace confinement (#15) -------------------------------------------


def _client(tmp_path: Path) -> _HarnessAcpClient:
    return _HarnessAcpClient(_runtime(tmp_path))


async def test_read_and_write_round_trip_inside_workspace(tmp_path: Path) -> None:
    client = _client(tmp_path)
    await client.write_text_file("s1", str(tmp_path / "sub" / "note.md"), "hello")
    assert (tmp_path / "sub" / "note.md").read_text() == "hello"
    resp = await client.read_text_file("s1", "sub/note.md")
    assert resp.content == "hello"


async def test_read_outside_workspace_is_refused(tmp_path: Path) -> None:
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("private")
    client = _client(tmp_path)
    with pytest.raises(PermissionError):
        await client.read_text_file("s1", str(secret))


async def test_traversal_outside_workspace_is_refused(tmp_path: Path) -> None:
    client = _client(tmp_path)
    with pytest.raises(PermissionError):
        await client.read_text_file("s1", "../../etc/passwd")


async def test_write_outside_workspace_is_refused(tmp_path: Path) -> None:
    client = _client(tmp_path)
    target = tmp_path.parent / "escaped.txt"
    with pytest.raises(PermissionError):
        await client.write_text_file("s1", str(target), "nope")
    assert not target.exists()


async def test_missing_file_raises_rather_than_returning_empty(tmp_path: Path) -> None:
    client = _client(tmp_path)
    with pytest.raises(OSError):
        await client.read_text_file("s1", "absent.md")
