"""Unit coverage for broker-compatible AsyncCodex construction."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from tests.unit.providers.codex.test_adapter import (
    FakeCodex,
    _config,  # pyright: ignore[reportPrivateUsage]
    _launch,  # pyright: ignore[reportPrivateUsage]
)

from talktoharnesses.domain.enums import ApprovalDecision
from talktoharnesses.domain.models import HarnessConfiguration
from talktoharnesses.providers.adapter import StartSessionRequest
from talktoharnesses.providers.codex import adapter as adapter_mod
from talktoharnesses.providers.codex.adapter import CodexAdapter
from talktoharnesses.providers.codex.compatibility import match_release


def test_build_broker_async_codex_forwards_public_approval_handler() -> None:
    def handler(method: str, params: dict[str, object] | None) -> dict[str, str]:
        del method, params
        return {"decision": "decline"}

    client = adapter_mod._build_broker_async_codex(handler)  # pyright: ignore[reportPrivateUsage]
    try:
        assert type(client).__name__ == "BrokerAsyncCodex"
        sync = client._client._sync  # pyright: ignore[reportPrivateUsage]
        assert sync._approval_handler is handler  # pyright: ignore[reportPrivateUsage]
    finally:
        # Construction must not start the process; still close transport state.
        close = getattr(client._client._sync, "close", None)  # pyright: ignore[reportPrivateUsage]
        if callable(close):
            close()


def test_sandbox_wire_value() -> None:
    from openai_codex.generated.v2_all import SandboxMode

    assert (
        adapter_mod._sandbox_wire_value(SimpleNamespace(value="workspace-write"))  # pyright: ignore[reportPrivateUsage]
        is SandboxMode.workspace_write
    )
    assert adapter_mod._sandbox_wire_value("read-only") is SandboxMode.read_only  # pyright: ignore[reportPrivateUsage]
    assert adapter_mod._sandbox_wire_value("full-access") is SandboxMode.danger_full_access  # pyright: ignore[reportPrivateUsage]


def test_codex_settings_modes() -> None:
    from openai_codex import ApprovalMode, Sandbox

    from talktoharnesses.domain.errors import DomainError

    mode, sandbox = adapter_mod._codex_settings("read_only")  # pyright: ignore[reportPrivateUsage]
    assert mode is ApprovalMode.auto_review
    assert sandbox is Sandbox.read_only
    _, full = adapter_mod._codex_settings("full-access")  # pyright: ignore[reportPrivateUsage]
    assert full is Sandbox.full_access
    with pytest.raises(DomainError):
        adapter_mod._codex_settings("not-a-mode")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_interrupt_and_close_cancel_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeCodex.instances.clear()

    async def fake_probe(config: HarnessConfiguration):
        release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr(adapter_mod, "probe_codex", fake_probe)
    adapter = CodexAdapter(client_factory=FakeCodex)
    await adapter.probe(_config())
    session = await adapter.start(
        StartSessionRequest(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            configuration=_config(),
            launch=_launch(),
        )
    )
    loop = __import__("asyncio").get_running_loop()
    fut = loop.create_future()
    adapter._pending_interactions[uuid4()] = fut  # pyright: ignore[reportPrivateUsage]
    await adapter.interrupt(session)
    assert fut.result() is ApprovalDecision.CANCEL

    fut2 = loop.create_future()
    adapter._pending_interactions[uuid4()] = fut2  # pyright: ignore[reportPrivateUsage]
    await adapter.close(session)
    assert fut2.result() is ApprovalDecision.CANCEL


@pytest.mark.asyncio
async def test_thread_start_override_forces_user_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(method: str, params: dict[str, object] | None) -> dict[str, str]:
        del method, params
        return {"decision": "accept"}

    client = adapter_mod._build_broker_async_codex(handler)  # pyright: ignore[reportPrivateUsage]
    started: dict[str, object] = {}

    async def fake_ensure() -> None:
        return None

    async def fake_thread_start(params: object) -> SimpleNamespace:
        started["params"] = params
        return SimpleNamespace(thread=SimpleNamespace(id="thr-1"))

    monkeypatch.setattr(client, "_ensure_initialized", fake_ensure)
    monkeypatch.setattr(client._client, "thread_start", fake_thread_start)  # pyright: ignore[reportPrivateUsage]

    thread = await client.thread_start(
        cwd="/tmp",
        model="m",
        sandbox=SimpleNamespace(value="workspace-write"),
    )
    assert thread.id == "thr-1"
    params = cast(SimpleNamespace, started["params"])
    assert params.approvals_reviewer.value == "user"
    assert params.cwd == "/tmp"


@pytest.mark.asyncio
async def test_thread_start_and_resume_yolo_uses_never_without_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = adapter_mod._build_broker_async_codex(None, yolo=True)  # pyright: ignore[reportPrivateUsage]
    started: dict[str, object] = {}
    resumed: dict[str, object] = {}

    async def fake_ensure() -> None:
        return None

    async def fake_thread_start(params: object) -> SimpleNamespace:
        started["params"] = params
        return SimpleNamespace(thread=SimpleNamespace(id="thr-yolo"))

    async def fake_thread_resume(thread_id: str, params: object) -> None:
        del thread_id
        resumed["params"] = params

    monkeypatch.setattr(client, "_ensure_initialized", fake_ensure)
    monkeypatch.setattr(client._client, "thread_start", fake_thread_start)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(client._client, "thread_resume", fake_thread_resume)  # pyright: ignore[reportPrivateUsage]

    thread = await client.thread_start(
        cwd="/tmp",
        model="m",
        sandbox=SimpleNamespace(value="workspace-write"),
    )
    assert thread.id == "thr-yolo"
    start_params = cast(SimpleNamespace, started["params"])
    assert start_params.approval_policy.root.value == "never"
    assert getattr(start_params, "approvals_reviewer", None) is None
    assert start_params.sandbox.value == "workspace-write"

    await client.thread_resume(
        "thr-yolo",
        cwd="/tmp",
        model="m",
        sandbox=SimpleNamespace(value="read-only"),
    )
    resume_params = cast(SimpleNamespace, resumed["params"])
    assert resume_params.approval_policy.root.value == "never"
    assert getattr(resume_params, "approvals_reviewer", None) is None
    assert resume_params.sandbox.value == "read-only"


def test_codex_settings_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from talktoharnesses.domain.errors import DomainError

    monkeypatch.setitem(sys.modules, "openai_codex", None)  # force ImportError on import
    with pytest.raises(DomainError) as exc:
        adapter_mod._codex_settings("default")  # pyright: ignore[reportPrivateUsage]
    assert exc.value.code.value == "provider_incompatible"
