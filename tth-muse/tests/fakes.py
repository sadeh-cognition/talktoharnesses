"""Fake MSP host and adapter factory for the contract suites."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from tth_types.adapter import HarnessAdapter
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, HarnessConfiguration

from tth_muse.harness import adapter as adapter_module
from tth_muse.harness.adapter import MuseAdapter
from tth_muse.runtime.handle import ProcessHandle

FLOOR_VERSION = "1.0.3-R2198.1"


class FakeMuseHost:
    """Scripted ``muse serve`` stdio peer speaking MSP v1.

    Every ``turn/start`` is admitted and completed immediately so a consumer
    always observes a terminal frame; ``pending`` seeds ``approval/listPending``.
    """

    def __init__(self, *, session_id: str = "native-session") -> None:
        self.frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.commands: list[dict[str, Any]] = []
        self.session_id = session_id
        self.pending: dict[str, list[dict[str, Any]]] = {"approvals": [], "userInputs": []}
        self.complete_turns = True

    async def emit(self, method: str, **params: Any) -> None:
        await self.frames.put(
            (json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode()
        )

    async def emit_raw(self, frame: dict[str, Any]) -> None:
        await self.frames.put((json.dumps(frame) + "\n").encode())

    async def write_stdin(self, data: bytes) -> None:
        frame: dict[str, Any] = json.loads(data)
        self.commands.append(frame)
        if "id" not in frame or "method" not in frame:
            return
        method: str = frame["method"]
        params: dict[str, Any] = frame.get("params") or {}
        result: dict[str, Any] = {"status": "accepted"}
        if method == "initialize":
            result = {
                "schema": {"version": 1},
                "sessionDurability": "durable",
                "serverInfo": {"name": "muse", "version": "1.0.3"},
            }
        elif method == "model/list":
            result = {"models": [{"modelId": "default", "displayLabel": "Default"}]}
        elif method in {"session/start", "session/resume"}:
            session_id: str = params.get("sessionId") or self.session_id
            result = {
                "session": {
                    "sessionId": session_id,
                    "workspaceRoot": params.get("workspaceRoot", "/tmp"),
                    "modelId": params.get("modelId", "default"),
                }
            }
        elif method == "approval/listPending":
            result = dict(self.pending)
        elif method == "turn/start":
            result.update(turnId=params["commandId"], disposition="started")
        if "commandId" in params and method not in {"session/start", "session/resume"}:
            result.setdefault("commandId", params["commandId"])
        await self.frames.put(
            (json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result}) + "\n").encode()
        )
        if method == "turn/start" and self.complete_turns:
            session_id = str(params["sessionId"])
            await self.emit("turn/started", sessionId=session_id, turnId=params["commandId"])
            await self.emit(
                "turn/completed",
                sessionId=session_id,
                turnId=params["commandId"],
                terminal="completed",
            )

    async def stdout(self) -> AsyncIterator[bytes]:
        while (frame := await self.frames.get()) is not None:
            yield frame

    async def close_stdin(self) -> None:
        await self.frames.put(None)


def capabilities_for(kind: HarnessKind) -> HarnessCapabilities:
    return HarnessCapabilities(
        kind=kind,
        version=FLOOR_VERSION,
        supports_resume=True,
        supports_steer=True,
        supports_interrupt=True,
    )


def config_for(kind: HarnessKind) -> HarnessConfiguration:
    return HarnessConfiguration(kind=kind, working_directory="/tmp", model="default")


def patch_probe(monkeypatch: Any) -> None:
    async def probe_muse(_config: HarnessConfiguration) -> HarnessCapabilities:
        return capabilities_for(HarnessKind.MUSE)

    monkeypatch.setattr(adapter_module, "probe_muse", probe_muse)


def make_adapter_factory(
    kind: HarnessKind,
    monkeypatch: Any,
) -> Callable[[], tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]]:
    assert kind is HarnessKind.MUSE
    patch_probe(monkeypatch)

    def factory() -> tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]:
        host = FakeMuseHost()
        adapter: HarnessAdapter = MuseAdapter()

        def bind_process(bound: HarnessAdapter) -> None:
            if not isinstance(bound, MuseAdapter):
                raise TypeError(f"adapter does not support bind_process: {type(bound)!r}")
            bound.bind_process(cast(ProcessHandle, cast(object, host)))

        return adapter, bind_process

    return factory
