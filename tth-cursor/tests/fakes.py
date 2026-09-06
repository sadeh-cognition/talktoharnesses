"""Fake native transports/SDK surfaces for the adapter contract suite."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from uuid import uuid4

from tth_types.adapter import HarnessAdapter
from tth_types.enums import HarnessKind
from tth_types.harness import HarnessCapabilities, HarnessConfiguration

from tth_cursor.harness.adapter import CursorAdapter
from tth_cursor.runtime.process_bound import ProcessBoundAdapter


def _option_get(options: object, key: str) -> object | None:
    getter = getattr(options, "get", None)
    if callable(getter):
        value = getter(key)
        return value if value is not None else None
    return getattr(options, key, None)


# ---------------------------------------------------------------------------
# Codex fake SDK
# ---------------------------------------------------------------------------


def _cursor_option(
    *,
    option_id: str,
    category: str,
    current: str,
    values: list[tuple[str, str]],
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": option_id,
        "category": category,
        "type": "select",
        "currentValue": current,
        "name": name or option_id,
        "options": [{"name": label, "value": value} for label, value in values],
    }


class _FakeAcpProcess:
    def __init__(
        self,
        *,
        agent_name: str = "grok",
        agent_version: str = "1.0.0",
        load_session: bool = True,
    ) -> None:
        self.process_id = uuid4()
        self.pid = 12345
        self.returncode: int | None = None
        self.forced = False
        self.forced_reason: str | None = None
        self.stderr_truncated = False
        self.retained_stderr_bytes = 0
        self.redacted_stderr_tail = ""
        self._stdout_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._stdout_taken = False
        self._next_id = 0
        self._session_id = f"acp-{uuid4()}"
        self._agent_name = agent_name
        self._agent_version = agent_version
        self._load_session = load_session
        self._task: asyncio.Task[None] | None = None
        self.requests: list[dict[str, Any]] = []
        self._cursor_mode = "agent"
        self._cursor_model = "default"
        self._cursor_params: dict[str, str] = {}

    @property
    def _is_cursor(self) -> bool:
        return self._agent_name == "cursor"

    async def write_stdin(self, data: bytes) -> None:
        line = data.decode("utf-8").strip()
        if not line:
            return
        msg = json.loads(line)
        if self._is_cursor:
            self.requests.append(msg)
        asyncio.create_task(self._respond(msg))

    def stdout(self) -> AsyncIterator[bytes]:
        if self._stdout_taken:
            raise RuntimeError("single consumer")
        self._stdout_taken = True

        async def _iter() -> AsyncIterator[bytes]:
            while True:
                item = await self._stdout_q.get()
                if item is None:
                    return
                yield item

        return _iter()

    def _cursor_config_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = [
            _cursor_option(
                option_id="model",
                category="model",
                current=self._cursor_model,
                values=[
                    ("Auto", "default"),
                    ("Composer 2.5", "composer-2.5"),
                    ("GPT-5.6 Sol", "gpt-5.6-sol"),
                ],
                name="Model",
            ),
            _cursor_option(
                option_id="mode",
                category="mode",
                current=self._cursor_mode,
                values=[
                    ("Agent", "agent"),
                    ("Plan", "plan"),
                    ("Ask", "ask"),
                ],
                name="Mode",
            ),
        ]
        if self._cursor_model == "composer-2.5":
            options.append(
                _cursor_option(
                    option_id="fast",
                    category="model_config",
                    current=self._cursor_params.get("fast", "false"),
                    values=[("Off", "false"), ("On", "true")],
                    name="Fast",
                )
            )
        elif self._cursor_model == "gpt-5.6-sol":
            options.append(
                _cursor_option(
                    option_id="context",
                    category="model_config",
                    current=self._cursor_params.get("context", "272k"),
                    values=[("272k", "272k"), ("1m", "1m")],
                    name="Context",
                )
            )
            options.append(
                _cursor_option(
                    option_id="reasoning",
                    category="thought_level",
                    current=self._cursor_params.get("reasoning", "medium"),
                    values=[("Low", "low"), ("Medium", "medium"), ("High", "high")],
                    name="Reasoning",
                )
            )
            options.append(
                _cursor_option(
                    option_id="fast",
                    category="model_config",
                    current=self._cursor_params.get("fast", "false"),
                    values=[("Off", "false"), ("On", "true")],
                    name="Fast",
                )
            )
        return options

    def _reset_cursor_params_for_model(self, model_id: str) -> None:
        self._cursor_model = model_id
        if model_id == "composer-2.5":
            self._cursor_params = {"fast": "false"}
        elif model_id == "gpt-5.6-sol":
            self._cursor_params = {
                "context": "272k",
                "reasoning": "medium",
                "fast": "false",
            }
        else:
            self._cursor_params = {}

    async def _respond(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            await self._reply(
                req_id,
                {
                    "protocolVersion": 1,
                    "agentInfo": {"name": self._agent_name, "version": self._agent_version},
                    "agentCapabilities": {"loadSession": self._load_session},
                },
            )
        elif method == "session/new":
            payload: dict[str, Any] = {"sessionId": self._session_id}
            if self._is_cursor:
                payload["configOptions"] = self._cursor_config_options()
            await self._reply(req_id, payload)
        elif method == "session/load":
            sid_obj = _option_get(msg.get("params") or {}, "sessionId")
            sid = sid_obj if isinstance(sid_obj, str) else self._session_id
            payload = {"sessionId": sid}
            if self._is_cursor:
                payload["configOptions"] = self._cursor_config_options()
            await self._reply(req_id, payload)
        elif method == "session/set_config_option" and self._is_cursor:
            await self._respond_set_config_option(req_id, msg.get("params") or {})
        elif method == "session/prompt":
            # Terminal with no assistant message content.
            await self._reply(req_id, {"stopReason": "end_turn"})
        elif method == "session/cancel":
            return
        elif req_id is not None:
            await self._reply(req_id, {})

    async def _respond_set_config_option(self, req_id: object, params: object) -> None:
        params_map: dict[str, Any] = (
            {str(k): v for k, v in cast(dict[object, object], params).items()}
            if isinstance(params, dict)
            else {}
        )
        config_id_obj = params_map.get("configId")
        value_obj = params_map.get("value")
        if not isinstance(config_id_obj, str) or not isinstance(value_obj, str):
            await self._reply_error(req_id, -32602, "invalid params")
            return
        config_id = config_id_obj
        value = value_obj
        current_options = self._cursor_config_options()
        match = next((item for item in current_options if item["id"] == config_id), None)
        if match is None:
            await self._reply_error(req_id, -32602, f"unknown configId: {config_id}")
            return
        advertised = {str(opt["value"]) for opt in cast(list[dict[str, Any]], match["options"])}
        if value not in advertised:
            await self._reply_error(req_id, -32602, f"unknown value for {config_id}: {value}")
            return
        if config_id == "model":
            self._reset_cursor_params_for_model(value)
        elif config_id == "mode":
            self._cursor_mode = value
        else:
            self._cursor_params[config_id] = value
        await self._reply(
            req_id,
            {
                "configOptions": self._cursor_config_options(),
            },
        )

    async def _reply(self, req_id: object, result: dict[str, Any]) -> None:
        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
        await self._stdout_q.put((payload + "\n").encode())

    async def _reply_error(self, req_id: object, code: int, message: str) -> None:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        )
        await self._stdout_q.put((payload + "\n").encode())

    async def close(self) -> None:
        self.returncode = 0
        await self._stdout_q.put(None)

    async def force_terminate(self, reason: str | None = None) -> None:
        self.forced = True
        self.forced_reason = reason
        self.returncode = -9
        await self._stdout_q.put(None)

    def events(self) -> AsyncIterator[object]:
        async def _gen() -> AsyncIterator[object]:
            if False:  # pragma: no cover
                yield None

        return _gen()


def _patch_probe(monkeypatch: Any, kind: HarnessKind) -> None:
    assert kind is HarnessKind.CURSOR

    async def probe_cursor(config: HarnessConfiguration):
        from tth_cursor.harness.compatibility import match_release

        release = match_release("2026.08.04-aaa8809", platform="linux")
        return release.to_harness_capabilities(), release

    monkeypatch.setattr("tth_cursor.harness.adapter.probe_cursor", probe_cursor)


def make_adapter_factory(
    kind: HarnessKind,
    monkeypatch: Any,
) -> Callable[[], tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]]:
    _patch_probe(monkeypatch, kind)

    def factory() -> tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]:
        proc = _FakeAcpProcess(
            agent_name="cursor",
            agent_version="2026.08.04-aaa8809",
        )
        adapter: HarnessAdapter = CursorAdapter()

        def bind_process(bound: HarnessAdapter) -> None:
            if not isinstance(bound, ProcessBoundAdapter):
                raise TypeError(f"adapter does not support bind_process: {type(bound)!r}")
            bound.bind_process(proc)  # type: ignore[arg-type]

        return adapter, bind_process

    return factory


def config_for(kind: HarnessKind) -> HarnessConfiguration:
    return HarnessConfiguration(
        kind=kind, working_directory="/tmp", model="composer-2.5[fast=false]", mode="ask"
    )


def capabilities_for(kind: HarnessKind) -> HarnessCapabilities:
    return HarnessCapabilities(kind=kind, version="test")
