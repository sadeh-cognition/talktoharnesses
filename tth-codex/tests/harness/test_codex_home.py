"""The CODEX_HOME gate serializes Codex starts until one succeeds."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from tth_types.adapter import ResumeSessionRequest, StartSessionRequest
from tth_types.harness import HarnessConfiguration

from tests.harness.fakes import FakeCodex, harness_config, launch_snapshot
from tth_codex.harness.codex_home import CodexHomeGate


async def _start(gate: CodexHomeGate, log: list[str], name: str, *, fail: bool = False) -> None:
    async with gate.start():
        log.append(f"{name}:enter")
        await asyncio.sleep(0.01)
        log.append(f"{name}:exit")
        if fail:
            raise RuntimeError("failed to initialize sqlite state runtime")


async def test_first_starts_run_alone_until_one_succeeds() -> None:
    gate = CodexHomeGate()
    log: list[str] = []

    results = await asyncio.gather(
        _start(gate, log, "a", fail=True),
        _start(gate, log, "b"),
        _start(gate, log, "c"),
        return_exceptions=True,
    )

    assert isinstance(results[0], RuntimeError)
    # The failed start leaves the gate closed, so the next one also runs alone.
    assert log[:4] == ["a:enter", "a:exit", "b:enter", "b:exit"]


async def test_starts_after_success_run_concurrently() -> None:
    gate = CodexHomeGate()
    log: list[str] = []
    await _start(gate, log, "first")
    log.clear()

    await asyncio.gather(*(_start(gate, log, name) for name in "abc"))

    assert log[:3] == ["a:enter", "b:enter", "c:enter"]


def _release():
    from tth_codex.harness.compatibility import match_release

    return match_release(sdk_version="0.154.0", runtime_version="0.154.0", platform="linux")


async def test_adapter_probes_share_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from tth_codex.harness import adapter as adapter_mod

    log: list[str] = []

    async def fake_probe(config: HarnessConfiguration):
        del config
        log.append("enter")
        await asyncio.sleep(0.01)
        log.append("exit")
        release = _release()
        return release.to_harness_capabilities(), release

    monkeypatch.setattr(adapter_mod, "probe_codex", fake_probe)
    gate = CodexHomeGate()
    adapters = [adapter_mod.CodexAdapter(client_factory=FakeCodex, home_gate=gate) for _ in "ab"]

    await asyncio.gather(*(adapter.probe(harness_config()) for adapter in adapters))

    assert log == ["enter", "exit", "enter", "exit"]


class _SlowCodex(FakeCodex):
    log: list[str] = []

    async def __aenter__(self) -> _SlowCodex:
        _SlowCodex.log.append("enter")
        await asyncio.sleep(0.01)
        _SlowCodex.log.append("entered")
        return self


async def test_adapter_start_and_resume_share_the_gate() -> None:
    from tth_codex.harness.adapter import CodexAdapter

    _SlowCodex.log = []
    gate = CodexHomeGate()
    starting = CodexAdapter(client_factory=_SlowCodex, home_gate=gate)
    resuming = CodexAdapter(client_factory=_SlowCodex, home_gate=gate)
    for adapter in (starting, resuming):
        adapter._release = _release()  # pyright: ignore[reportPrivateUsage]

    await asyncio.gather(
        starting.start(
            StartSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=harness_config(),
                launch=launch_snapshot(),
            )
        ),
        resuming.resume(
            ResumeSessionRequest(
                conversation_id=uuid4(),
                binding_id=uuid4(),
                configuration=harness_config(),
                native_session_id="thread-1",
                launch=launch_snapshot(),
            )
        ),
    )

    assert _SlowCodex.log == ["enter", "entered", "enter", "entered"]
