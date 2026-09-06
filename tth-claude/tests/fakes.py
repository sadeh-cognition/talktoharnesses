"""Fake native transports/SDK surfaces for the adapter contract suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from tth_types.adapter import HarnessAdapter
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, HarnessConfiguration

from tth_claude.harness.adapter import ClaudeAdapter


def _option_get(options: object, key: str) -> object | None:
    getter = getattr(options, "get", None)
    if callable(getter):
        value = getter(key)
        return value if value is not None else None
    return getattr(options, key, None)


# ---------------------------------------------------------------------------
# Codex fake SDK
# ---------------------------------------------------------------------------


@dataclass
class _FakeClaude:
    options: object
    session_id: str = ""
    interrupted: bool = False
    disconnected: bool = False

    def __post_init__(self) -> None:
        session_id = _option_get(self.options, "session_id")
        resume = _option_get(self.options, "resume")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        elif isinstance(resume, str) and resume:
            self.session_id = resume
        elif not self.session_id:
            self.session_id = f"claude-{uuid4()}"

    async def connect(self, prompt: object | None = None) -> None:
        del prompt

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        del prompt, session_id

    def receive_response(self) -> AsyncIterator[dict[str, object]]:
        async def _gen() -> AsyncIterator[dict[str, object]]:
            yield {
                "type": "result",
                "subtype": "success",
                "session_id": self.session_id,
                "is_error": False,
                "stop_reason": "end_turn",
            }

        return _gen()

    async def interrupt(self) -> None:
        self.interrupted = True


# ---------------------------------------------------------------------------
# OpenCode fake HTTP
# ---------------------------------------------------------------------------


def _patch_probe(monkeypatch: Any, kind: HarnessKind) -> None:
    assert kind is HarnessKind.CLAUDE

    async def probe_claude(config: HarnessConfiguration):
        from tth_claude.harness.compatibility import match_release

        release = match_release(
            sdk_version="0.1.53",
            cli_version="2.1.88",
            cli_source="bundled",
            platform="linux",
        )
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_claude.harness.adapter.probe_claude", probe_claude)


def make_adapter_factory(
    kind: HarnessKind,
    monkeypatch: Any,
) -> Callable[[], tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]]:
    _patch_probe(monkeypatch, kind)

    def factory() -> tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]:
        adapter: HarnessAdapter = ClaudeAdapter(client_factory=_FakeClaude)
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
