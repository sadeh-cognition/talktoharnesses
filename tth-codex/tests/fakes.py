"""Fake native transports/SDK surfaces for the adapter contract suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from tth_types.adapter import HarnessAdapter
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, HarnessConfiguration

from tth_codex.harness.adapter import CodexAdapter


@dataclass
class _CodexTurn:
    id: str
    thread_id: str
    prompt: str
    steered: list[str] = field(default_factory=list[str])
    interrupted: bool = False

    def stream(self) -> AsyncIterator[dict[str, object]]:
        async def _gen() -> AsyncIterator[dict[str, object]]:
            yield {
                "method": "turnCompleted",
                "thread_id": self.thread_id,
                "turn_id": self.id,
                "status": "completed",
                "final_response": None,
            }

        return _gen()

    async def steer(self, prompt: str) -> None:
        self.steered.append(str(prompt))

    async def interrupt(self) -> None:
        self.interrupted = True


@dataclass
class _CodexThread:
    id: str

    async def turn(self, prompt: str) -> _CodexTurn:
        return _CodexTurn(id=f"turn-{uuid4()}", thread_id=self.id, prompt=str(prompt))


class _FakeCodex:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> _FakeCodex:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True

    async def close(self) -> None:
        self.closed = True

    async def thread_start(self, **kwargs: object) -> _CodexThread:
        del kwargs
        return _CodexThread(id=f"codex-{uuid4()}")

    async def thread_resume(self, thread_id: str, **kwargs: object) -> _CodexThread:
        del kwargs
        return _CodexThread(id=thread_id)


# ---------------------------------------------------------------------------
# Claude fake SDK
# ---------------------------------------------------------------------------


def _patch_probe(monkeypatch: Any, kind: HarnessKind) -> None:
    assert kind is HarnessKind.CODEX

    async def probe_codex(config: HarnessConfiguration):
        from tth_codex.harness.compatibility import match_release

        release = match_release(sdk_version="0.144.4", runtime_version="0.144.4", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_codex.harness.adapter.probe_codex", probe_codex)


def make_adapter_factory(
    kind: HarnessKind,
    monkeypatch: Any,
) -> Callable[[], tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]]:
    _patch_probe(monkeypatch, kind)

    def factory() -> tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]:
        adapter: HarnessAdapter = CodexAdapter(client_factory=_FakeCodex)
        return adapter, _noop_bind

    return factory


def config_for(kind: HarnessKind) -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=kind, working_directory="/tmp", model="default", mode="default"
    )


def _noop_bind(_adapter: HarnessAdapter) -> None:
    return None


def capabilities_for(kind: HarnessKind) -> HarnessCapabilities:
    return HarnessCapabilities(kind=kind, version="test")
