"""Fake native transports/SDK surfaces for the common adapter contract suite."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from json import dumps
from typing import Any, cast
from uuid import uuid4

from talktoharnesses.domain.enums import HarnessKind
from talktoharnesses.domain.models import HarnessCapabilities, HarnessConfiguration
from talktoharnesses.providers._process_bound import ProcessBoundAdapter
from talktoharnesses.providers.adapter import HarnessAdapter
from talktoharnesses.providers.claude.adapter import ClaudeAdapter
from talktoharnesses.providers.codex.adapter import CodexAdapter
from talktoharnesses.providers.cursor.adapter import CursorAdapter
from talktoharnesses.providers.grok.adapter import GrokAdapter
from talktoharnesses.providers.opencode.adapter import OpenCodeAdapter
from talktoharnesses.providers.prime_agent.adapter import PrimeAgentAdapter


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


@dataclass
class _HttpResponse:
    status_code: int
    body: Any = None
    chunks: list[bytes] = field(default_factory=list[bytes])

    def json(self) -> Any:
        return self.body

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def __aenter__(self) -> _HttpResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@dataclass
class _OpenStreamResponse(_HttpResponse):
    """SSE response that stays open until the consumer is cancelled."""

    events: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue[bytes]())

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        while True:
            yield await self.events.get()


class _FakeOpenCodeHttp:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.session_id = f"oc-{uuid4()}"
        self.closed = False
        self.events: asyncio.Queue[bytes] = asyncio.Queue()

    async def get(self, path: str) -> _HttpResponse:
        if path == "/global/health":
            return _HttpResponse(200, {"healthy": True, "version": "1.2.27"})
        if path.startswith("/session/"):
            sid = path.rsplit("/", 1)[-1]
            if sid != self.session_id and sid != self.session_id:
                # Allow resume of known id only; create path sets session_id.
                return _HttpResponse(200, {"id": sid})
            return _HttpResponse(200, {"id": sid})
        return _HttpResponse(404, {})

    async def post(self, path: str, json: dict[str, Any] | None = None) -> _HttpResponse:
        del json
        if path == "/session":
            return _HttpResponse(200, {"id": self.session_id})
        if path.endswith("/prompt_async"):
            payload = {
                "type": "session.status",
                "properties": {"sessionID": self.session_id, "status": {"type": "idle"}},
            }
            self.events.put_nowait(f"data: {dumps(payload)}\n\n".encode())
        return _HttpResponse(200, {"id": "ok"})

    def stream(self, method: str, path: str) -> _HttpResponse:
        del method, path
        payload = b'{"type":"server.connected"}'
        # Keep stream open after the connected event so the SSE task is not torn down
        # before the adapter finishes start/submit.
        return _OpenStreamResponse(
            200,
            chunks=[b"data: " + payload + b"\n\n"],
            events=self.events,
        )

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# ACP fake process for Grok/Cursor
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
                    "agentCapabilities": {"loadSession": True},
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


class _FakePrimeProcess(_FakeAcpProcess):
    def __init__(self) -> None:
        super().__init__()
        self._session_file = f"/tmp/prime-{uuid4()}.jsonl"

    async def _respond(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("id")
        command = msg.get("type")
        if command == "switch_session":
            selected = msg.get("sessionPath")
            if isinstance(selected, str):
                self._session_file = selected
        response: dict[str, Any] = {
            "id": request_id,
            "type": "response",
            "command": command,
            "success": True,
        }
        if command == "get_state":
            response["data"] = {
                "sessionFile": self._session_file,
                "sessionId": self._session_file.rsplit("/", 1)[-1].removesuffix(".jsonl"),
            }
        await self._stdout_q.put((json.dumps(response) + "\n").encode())
        if command == "prompt":
            await self._stdout_q.put(b'{"type":"agent_end","messages":[]}\n')

    async def close_stdin(self) -> None:
        await self.close()


def _patch_probe(monkeypatch: Any, kind: HarnessKind) -> None:
    if kind is HarnessKind.CODEX:

        async def probe_codex(config: HarnessConfiguration):
            from talktoharnesses.providers.codex.compatibility import match_release

            release = match_release(
                sdk_version="0.144.4", runtime_version="0.144.4", platform="linux"
            )
            return release.to_harness_capabilities(), release

        monkeypatch.setattr("talktoharnesses.providers.codex.adapter.probe_codex", probe_codex)
    elif kind is HarnessKind.CLAUDE:

        async def probe_claude(config: HarnessConfiguration):
            from talktoharnesses.providers.claude.compatibility import match_release

            release = match_release(
                sdk_version="0.1.53",
                cli_version="2.1.88",
                cli_source="bundled",
                platform="linux",
            )
            return release.to_harness_capabilities(), release

        monkeypatch.setattr("talktoharnesses.providers.claude.adapter.probe_claude", probe_claude)
    elif kind is HarnessKind.OPENCODE:

        async def probe_opencode(config: HarnessConfiguration):
            from talktoharnesses.providers.opencode.compatibility import match_release

            release = match_release("1.2.27", platform="linux")
            return release.to_harness_capabilities(), release

        monkeypatch.setattr(
            "talktoharnesses.providers.opencode.adapter.probe_opencode", probe_opencode
        )
    elif kind is HarnessKind.PRIME_AGENT:

        async def probe_prime_agent(config: HarnessConfiguration):
            from talktoharnesses.providers.prime_agent.compatibility import match_release

            release = match_release("0.7.1", platform="linux")
            return release.to_harness_capabilities(), release

        monkeypatch.setattr(
            "talktoharnesses.providers.prime_agent.adapter.probe_prime_agent",
            probe_prime_agent,
        )
    elif kind is HarnessKind.GROK:

        async def probe_grok(config: HarnessConfiguration):
            from talktoharnesses.providers.grok.compatibility import match_release

            release = match_release("grok 1.0.0 (3cd0d0cbce)", platform="linux")
            return release.to_harness_capabilities(), release

        monkeypatch.setattr("talktoharnesses.providers.grok.adapter.probe_grok", probe_grok)
    elif kind is HarnessKind.CURSOR:

        async def probe_cursor(config: HarnessConfiguration):
            from talktoharnesses.providers.cursor.compatibility import match_release

            release = match_release("2026.08.04-aaa8809", platform="linux")
            return release.to_harness_capabilities(), release

        monkeypatch.setattr("talktoharnesses.providers.cursor.adapter.probe_cursor", probe_cursor)


def _noop_bind(_adapter: HarnessAdapter) -> None:
    return None


def make_adapter_factory(
    kind: HarnessKind,
    monkeypatch: Any,
) -> Callable[[], tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]]:
    """Return a zero-arg factory producing (adapter, bind_fn)."""

    _patch_probe(monkeypatch, kind)

    def factory() -> tuple[HarnessAdapter, Callable[[HarnessAdapter], Any]]:
        if kind is HarnessKind.CODEX:
            adapter: HarnessAdapter = CodexAdapter(client_factory=_FakeCodex)
            return adapter, _noop_bind
        if kind is HarnessKind.CLAUDE:
            adapter = ClaudeAdapter(client_factory=_FakeClaude)
            return adapter, _noop_bind
        if kind is HarnessKind.OPENCODE:
            adapter = OpenCodeAdapter(http_client_factory=_FakeOpenCodeHttp)
            adapter.prepare_port(18080)
            return adapter, _noop_bind
        if kind is HarnessKind.PRIME_AGENT:
            proc = _FakePrimeProcess()
            adapter = PrimeAgentAdapter()

            def bind_prime(bound: HarnessAdapter) -> None:
                assert isinstance(bound, ProcessBoundAdapter)
                bound.bind_process(proc)  # type: ignore[arg-type]

            return adapter, bind_prime
        # Grok / Cursor process-bound ACP
        if kind is HarnessKind.CURSOR:
            proc = _FakeAcpProcess(
                agent_name="cursor",
                agent_version="2026.08.04-aaa8809",
            )
            adapter = CursorAdapter()
        else:
            proc = _FakeAcpProcess()
            adapter = GrokAdapter()

        def bind_process(bound: HarnessAdapter) -> None:
            if not isinstance(bound, ProcessBoundAdapter):
                raise TypeError(f"adapter does not support bind_process: {type(bound)!r}")
            bound.bind_process(proc)  # type: ignore[arg-type]

        return adapter, bind_process

    return factory


def config_for(kind: HarnessKind) -> HarnessConfiguration:
    executable = None
    if kind in {
        HarnessKind.GROK,
        HarnessKind.CURSOR,
        HarnessKind.OPENCODE,
        HarnessKind.PRIME_AGENT,
    }:
        executable = "/bin/true"
    if kind is HarnessKind.CURSOR:
        model: str | None = "composer-2.5[fast=false]"
        mode: str | None = "ask"
    elif kind in {HarnessKind.OPENCODE, HarnessKind.PRIME_AGENT}:
        model = "test/default"
        mode = "high" if kind is HarnessKind.PRIME_AGENT else "default"
    else:
        model = "default"
        mode = "default"
    return HarnessConfiguration(
        kind=kind,
        executable_path=executable,
        working_directory="/tmp",
        model=model,
        mode=mode,
    )


def capabilities_for(kind: HarnessKind) -> HarnessCapabilities:
    return HarnessCapabilities(kind=kind, version="test")
