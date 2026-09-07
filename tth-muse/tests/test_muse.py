"""Muse protocol behavior, using Meta's recorded MSP frames and a fake host."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from tth_types.adapter import (
    HarnessInteractionRequest,
    HarnessSession,
    ResumeSessionRequest,
    StartSessionRequest,
    SteerRequest,
    TurnRequest,
)
from tth_types.enums import ApprovalDecision, HarnessKind
from tth_types.errors import DomainError
from tth_types.events import AssistantMessageCompletedPayload, HarnessEvent, UsageUpdatedPayload
from tth_types.harness import (
    HarnessCapabilities,
    HarnessConfiguration,
    HarnessMcpHeader,
    HarnessMcpServer,
    InteractionAnswer,
    LaunchSnapshot,
)

from tth_muse.harness import adapter as adapter_module
from tth_muse.harness import config_dir as config_dir_module
from tth_muse.harness import connection as connection_module
from tth_muse.harness.adapter import MuseAdapter
from tth_muse.harness.compatibility import compare_versions
from tth_muse.harness.config_dir import (
    remove_config_dir,
    render_config_dir,
    settings_with_mcp_servers,
)
from tth_muse.harness.connection import MuseConnection
from tth_muse.harness.normalizer import MuseNormalizer
from tth_muse.harness.probe import build_argv
from tth_muse.runtime.handle import ProcessHandle


def fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())


def test_official_transcript_streams_once_and_reports_usage_before_terminal() -> None:
    normalizer = MuseNormalizer()
    frames = fixture("text-run-single-turn")
    session = next(
        frame["params"]["session"] for frame in frames if frame.get("method") == "session/started"
    )
    normalizer.session_id = session["sessionId"]
    normalizer.begin_turn(uuid4())
    events: list[HarnessEvent] = []
    for frame in frames:
        if "method" in frame:
            events.extend(normalizer.on_notification(frame["method"], frame["params"]))
            assert normalizer.on_notification(frame["method"], frame["params"]) == []
    messages = [event for event in events if isinstance(event, AssistantMessageCompletedPayload)]
    assert len(messages) == 1
    assert messages[0].text == "All 214 tests pass except two in tbh-agent..."
    assert events[-2].type == "usage_updated"
    assert events[-1].type == "turn_completed"
    usage = events[-2]
    assert usage.input_tokens == 48210 and usage.cached_input_tokens == 40100
    assert usage.total_tokens is None  # The aggregate doesn't report a counted-once total.


def test_usage_is_per_turn_and_uses_counted_once_provider_values() -> None:
    normalizer = MuseNormalizer()
    normalizer.session_id = "session"
    for _ in range(2):
        normalizer.begin_turn(uuid4())
        events: list[HarnessEvent] = []
        for _index in range(2):
            events = normalizer.on_notification(
                "session/tokenUsage",
                {
                    "sessionId": "session",
                    "usage": {"inputTokens": 900, "cachedTokens": 500, "outputTokens": 10},
                    "promptTokens": 100,
                    "totalTokens": 110,
                },
            )
        usage = cast(UsageUpdatedPayload, events[0])
        assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (200, 20, 220)
        assert usage.cached_input_tokens == 1000


def test_secrets_split_across_deltas_are_redacted() -> None:
    normalizer = MuseNormalizer()
    normalizer.session_id = "session"
    normalizer.patterns = ("secret",)
    normalizer.begin_turn(uuid4())
    item = {"itemId": "item", "kind": "agentMessage", "text": ""}
    normalizer.on_notification("item/started", {"sessionId": "session", "item": item})
    events: list[HarnessEvent] = []
    for delta in ("a sec", "ret here"):
        events.extend(
            normalizer.on_notification(
                "item/delta",
                {
                    "sessionId": "session",
                    "itemId": "item",
                    "field": "text",
                    "delta": delta,
                },
            )
        )
    events.extend(
        normalizer.on_notification(
            "item/completed",
            {
                "sessionId": "session",
                "item": {**item, "text": "a secret here"},
            },
        )
    )
    deltas = "".join(
        getattr(event, "text", "") for event in events if event.type == "assistant_message_delta"
    )
    assert deltas == "a [REDACTED] here"
    assert "secret" not in "".join(event.model_dump_json() for event in events)


def test_residual_native_turn_cannot_replace_or_complete_the_queued_turn() -> None:
    normalizer = MuseNormalizer()
    normalizer.session_id = "session"
    normalizer.begin_turn(uuid4())
    normalizer.native_turn_id = "queued-turn"
    residual = {"sessionId": "session", "turnId": "steer-continuation"}
    assert normalizer.on_notification("turn/started", residual) == []
    assert (
        normalizer.on_notification(
            "session/tokenUsage",
            {
                **residual,
                "usage": {"outputTokens": 100},
                "totalTokens": 200,
            },
        )
        == []
    )
    assert (
        normalizer.on_notification(
            "item/completed",
            {
                "sessionId": "session",
                "item": {
                    "itemId": "residual-item",
                    "kind": "agentMessage",
                    "turnId": "steer-continuation",
                    "text": "wrong turn",
                },
            },
        )
        == []
    )
    assert normalizer.on_notification("turn/completed", {**residual, "terminal": "completed"}) == []
    assert normalizer.native_turn_id == "queued-turn"
    assert normalizer.turn_id is not None
    events = normalizer.on_notification(
        "turn/completed",
        {
            "sessionId": "session",
            "turnId": "queued-turn",
            "terminal": "cancelled",
        },
    )
    assert [event.type for event in events] == ["turn_interrupted"]


@pytest.mark.parametrize(
    "status,outcome",
    [
        ("completed", "success"),
        ("failed", "failure"),
        ("cancelled", "cancelled"),
        ("new-terminal-state", "unknown"),
    ],
)
def test_tool_terminal_preserves_native_outcome_and_output(status: str, outcome: str) -> None:
    normalizer = MuseNormalizer()
    normalizer.session_id = "session"
    normalizer.begin_turn(uuid4())
    events = normalizer.on_notification(
        "item/completed",
        {
            "sessionId": "session",
            "item": {
                "itemId": "tool",
                "kind": "toolCall",
                "status": status,
                "tool": "bash",
                "visibleOutput": "command output",
            },
        },
    )
    completed = events[-1].model_dump(mode="json")
    assert completed["outcome"] == outcome
    assert completed["output_tail"] == "command output"
    # Official transcripts carry the name under ``tool`` (see fixtures).
    assert {event.model_dump(mode="json")["tool_name"] for event in events} == {"bash"}


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("1.0.3-R9.1", "1.0.3-R10.1", -1),
        ("1.0.4-R1.1", "1.0.3-R2198.1", 1),
        ("1.0.3-R2198.1", "1.0.3-R2198.1", 0),
    ],
)
def test_release_order_includes_numeric_build(left: str, right: str, expected: int) -> None:
    assert compare_versions(left, right) == expected


class Host:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.commands: list[dict[str, Any]] = []
        self.schema_version = 1
        self.fail_method: str | None = None
        self.session_id = "native-session"
        self.queued = False
        self.errors: list[dict[str, Any]] = []
        self.ack_command_id: str | None = None

    async def emit(self, method: str, **params: Any) -> None:
        await self.frames.put(
            (json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode()
        )

    async def write_stdin(self, data: bytes) -> None:
        frame = json.loads(data)
        self.commands.append(frame)
        if "id" not in frame or "method" not in frame:
            return
        method, params = frame["method"], frame["params"]
        if method not in {"initialize", "model/list", "approval/listPending"}:
            assert UUID(params["commandId"]).version == 7
        result: dict[str, Any] = {"status": "accepted"}
        if "commandId" in params:
            result["commandId"] = self.ack_command_id or params["commandId"]
        if method == "initialize":
            result = {"schema": {"version": self.schema_version}}
        elif method in {"session/start", "session/resume"}:
            result = {
                "session": {
                    "sessionId": self.session_id,
                    "workspaceRoot": "/tmp",
                    "modelId": "default",
                }
            }
        elif method == "approval/listPending":
            result = {"approvals": [], "userInputs": []}
        elif method == "turn/start":
            result.update(
                turnId=params["commandId"], disposition="queued" if self.queued else "started"
            )
        response = {"jsonrpc": "2.0", "id": frame["id"], "result": result}
        if self.errors:
            response = {"jsonrpc": "2.0", "id": frame["id"], "error": self.errors.pop(0)}
        if method == self.fail_method:
            response = {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "error": {"code": -1, "message": "rejected"},
            }
        await self.frames.put((json.dumps(response) + "\n").encode())
        if method == "turn/unqueue":
            await self.emit("turn/unqueued", sessionId=self.session_id, turnId=params["turnId"])

    async def stdout(self) -> AsyncIterator[bytes]:
        while (frame := await self.frames.get()) is not None:
            yield frame

    async def close_stdin(self) -> None:
        await self.frames.put(None)


async def start(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resume: bool = False,
    adapter_factory: Callable[[], MuseAdapter] = MuseAdapter,
    host: Host | None = None,
) -> tuple[MuseAdapter, Any, Host]:
    caps = HarnessCapabilities(
        kind=HarnessKind.MUSE,
        version="1.0.3-R2198.1",
        supports_resume=True,
        supports_steer=True,
        supports_interrupt=True,
    )

    async def probe(_config: HarnessConfiguration) -> HarnessCapabilities:
        return caps

    monkeypatch.setattr(adapter_module, "probe_muse", probe)
    adapter, host = adapter_factory(), host or Host()
    config = HarnessConfiguration(kind=HarnessKind.MUSE, working_directory="/tmp", model="default")
    await adapter.probe(config)
    adapter.bind_process(cast(ProcessHandle, cast(object, host)))
    request = StartSessionRequest(
        conversation_id=uuid4(),
        binding_id=uuid4(),
        configuration=config,
        launch=LaunchSnapshot(
            harness_version=caps.version,
            working_directory="/tmp",
            capabilities=caps,
            adapter_version="test",
        ),
    )
    if resume:
        session = await adapter.resume(
            ResumeSessionRequest(**request.model_dump(), native_session_id=host.session_id)
        )
    else:
        session = await adapter.start(request)
    return adapter, session, host


async def test_create_resume_model_reset_steer_cancel_and_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, session, host = await start(monkeypatch, resume=True)
    try:
        turn = TurnRequest(turn_id=uuid4(), prompt="hello", model="other")
        await adapter.submit(session, turn)
        with pytest.raises(DomainError):
            await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="busy"))
        await adapter.submit(session, turn)
        assert sum(frame["method"] == "turn/start" for frame in host.commands) == 1
        await adapter.steer(session, SteerRequest(turn_id=turn.turn_id, prompt="focus"))
        await adapter.interrupt(session)
        await host.emit("turn/completed", sessionId=host.session_id, terminal="cancelled")
        event = await asyncio.wait_for(anext(adapter.events(session)), 1)
        assert getattr(event, "type", None) == "turn_interrupted"
        await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="next"))
        changes = [
            frame["params"]["model"]["modelId"]
            for frame in host.commands
            if frame["method"] == "session/setModel"
        ]
        assert changes == ["other", "default"]
        await host.close_stdin()
        event = await asyncio.wait_for(anext(adapter.events(session)), 1)
        assert getattr(event, "type", None) == "turn_outcome_unknown"
    finally:
        await adapter.close(session)


@pytest.mark.parametrize(
    "name,method",
    [
        ("approval-round-trip", "approval/requested"),
        ("userinput-answer-round-trip", "userInput/requested"),
    ],
)
@pytest.mark.parametrize("failed", [False, True])
async def test_official_interactions_round_trip(
    monkeypatch: pytest.MonkeyPatch, name: str, method: str, failed: bool
) -> None:
    adapter, session, host = await start(monkeypatch)
    try:
        await adapter.submit(session, TurnRequest(turn_id=uuid4(), prompt="ask"))
        params: dict[str, Any] = next(
            frame["params"]
            for frame in fixture(name)
            if frame.get("method") in {method, method.removesuffix("ed")}
        )
        params = {
            **params,
            "sessionId": host.session_id,
            "turnId": next(
                frame["params"]["commandId"]
                for frame in host.commands
                if frame.get("method") == "turn/start"
            ),
        }
        await host.frames.put(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 18,
                        "method": method.removesuffix("ed"),
                        "params": params,
                    }
                )
                + "\n"
            ).encode()
        )
        if method == "approval/requested":
            await host.emit(method, **params)
        interaction = await asyncio.wait_for(anext(adapter.events(session)), 1)
        assert isinstance(interaction, HarnessInteractionRequest)
        assert {"jsonrpc": "2.0", "id": 18, "result": {}} in host.commands
        if method == "approval/requested":
            answer = InteractionAnswer(
                interaction_id=interaction.payload.interaction_id,
                decision=ApprovalDecision.ALLOW_ONCE,
            )
            command = "approval/decide"
        else:
            answer = InteractionAnswer(
                interaction_id=interaction.payload.interaction_id,
                answers={row["id"]: row["options"][0]["label"] for row in params["questions"]},
            )
            command = "userInput/answer"
        host.fail_method = command if failed else None
        outcomes = await asyncio.gather(
            adapter.answer_interaction(session, answer),
            adapter.answer_interaction(session, answer),
            return_exceptions=True,
        )
        if failed:
            assert all(isinstance(outcome, DomainError) for outcome in outcomes)
            host.fail_method = None
            with pytest.raises(DomainError):
                await adapter.answer_interaction(session, answer)
        else:
            assert outcomes == [None, None]
            await adapter.answer_interaction(session, answer)
        assert sum(frame.get("method") == command for frame in host.commands) == 1
        sent = host.commands[-1]
        assert sent["method"] == command
        if command == "approval/decide":
            assert sent["params"]["requirementId"] == params["currentRequirementId"]
            assert sent["params"]["choiceId"] == next(
                row["choiceId"]
                for row in params["availableChoices"]
                if row["decision"] == "approved"
            )
        else:
            assert (
                sent["params"]["answers"][0]["selectedLabel"]
                == params["questions"][0]["options"][0]["label"]
            )
        await host.emit(method, **params)
        await host.emit(
            "turn/completed",
            sessionId=host.session_id,
            turnId=params["turnId"],
            terminal="completed",
        )
        event = await asyncio.wait_for(anext(adapter.events(session)), 1)
        assert getattr(event, "type", None) == "turn_completed"
    finally:
        await adapter.close(session)


async def test_protocol_mismatch_fails_closed() -> None:
    host = Host()
    host.schema_version = 2

    async def ignore(*_args: object) -> None:
        pass

    connection = MuseConnection(cast(ProcessHandle, cast(object, host)), ignore, ignore)
    try:
        with pytest.raises(DomainError, match="MSP v1"):
            await connection.initialize()
    finally:
        await connection.close()


async def test_command_ids_remain_ordered_through_clock_rollback_and_sequence_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ignore(*_args: object) -> None:
        pass

    host = Host()
    connection = MuseConnection(cast(ProcessHandle, cast(object, host)), ignore, ignore)
    try:
        monkeypatch.setattr(connection_module.time, "time_ns", lambda: 1_000_000_000)
        ids = [UUID(connection.mint_command_id()) for _ in range(4097)]
        monkeypatch.setattr(connection_module.time, "time_ns", lambda: 900_000_000)
        ids.append(UUID(connection.mint_command_id()))
        assert all(identity.version == 7 for identity in ids)
        assert all(left.int < right.int for left, right in zip(ids, ids[1:], strict=False))
        assert ids[0].int >> 80 == 1000
        assert ids[-1].int >> 80 == 1001
    finally:
        await connection.close()


async def test_approval_server_request_is_only_a_receipt_acknowledgment() -> None:
    received: list[str] = []

    async def notification(name: str, _params: dict[str, Any]) -> None:
        received.append(name)

    async def disconnected(_message: str) -> None:
        pass

    host = Host()
    connection = MuseConnection(cast(ProcessHandle, cast(object, host)), notification, disconnected)
    try:
        await host.frames.put(
            (json.dumps({"jsonrpc": "2.0", "id": 18, "method": "approval/request"}) + "\n").encode()
        )
        await connection.request("model/list")
        assert {"jsonrpc": "2.0", "id": 18, "result": {}} in host.commands
        assert received == []
        await host.emit("approval/requested")
        await connection.request("model/list")
        assert received == ["approval/requested"]
    finally:
        await connection.close()


@pytest.mark.parametrize(
    "code,kind,retry",
    [
        (-32001, "overloaded", True),
        (-32031, "backpressured", True),
        (-32603, "internal", False),
        (-32001, "internal", False),
    ],
)
async def test_approval_command_retries_only_sdk_non_admission_errors(
    code: int, kind: str, retry: bool
) -> None:
    async def ignore(*_args: object) -> None:
        pass

    host = Host()
    host.errors = [
        {"code": code, "message": "not settled", "data": {"kind": kind, "retryable": True}}
    ]
    connection = MuseConnection(cast(ProcessHandle, cast(object, host)), ignore, ignore)
    try:
        params = {
            "sessionId": "session",
            "approvalId": "approval",
            "requirementId": {"approvalId": "approval", "sourceIndex": 0},
            "choiceId": "allow_once",
        }
        if retry:
            await connection.command("approval/decide", **params)
            first, second = host.commands
            assert first["params"] == second["params"]
            assert first["id"] != second["id"]
            host.commands.clear()
            host.errors = [{"code": code, "message": "still full", "data": {"kind": kind}}] * 3
            with pytest.raises(DomainError, match="still full"):
                await connection.command("approval/decide", **params)
            assert len(host.commands) == 3
            assert len({row["params"]["commandId"] for row in host.commands}) == 1
        else:
            with pytest.raises(DomainError, match="not settled"):
                await connection.command("approval/decide", **params)
            assert len(host.commands) == 1
    finally:
        await connection.close()


async def test_approval_acknowledgment_must_echo_its_command() -> None:
    async def ignore(*_args: object) -> None:
        pass

    host = Host()
    host.ack_command_id = str(uuid4())
    connection = MuseConnection(cast(ProcessHandle, cast(object, host)), ignore, ignore)
    try:
        with pytest.raises(DomainError, match="commandId mismatch"):
            await connection.command("approval/decide", approvalId="approval")
        assert len(host.commands) == 1
    finally:
        await connection.close()


async def test_interrupt_reclaims_native_queued_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, session, host = await start(monkeypatch)
    try:
        host.queued = True
        turn = TurnRequest(turn_id=uuid4(), prompt="queued behind native continuation")
        await adapter.submit(session, turn)
        assert not await adapter.steer(session, SteerRequest(turn_id=turn.turn_id, prompt="later"))
        await adapter.interrupt(session)
        event = await asyncio.wait_for(anext(adapter.events(session)), 1)
        assert getattr(event, "type", None) == "turn_interrupted"
        assert host.commands[-1]["method"] == "turn/unqueue"
    finally:
        await adapter.close(session)


def test_yolo_changes_host_sandbox_only_when_requested() -> None:
    config = HarnessConfiguration(kind=HarnessKind.MUSE, working_directory="/tmp")
    assert build_argv(config) == ("serve",)
    assert build_argv(config.model_copy(update={"yolo": True})) == (
        "serve",
        "--disable-sandbox",
        "--trust-workspace",
    )


def _mcp_config(**overrides: object) -> HarnessConfiguration:
    fields: dict[str, object] = {
        "kind": HarnessKind.MUSE,
        "working_directory": "/tmp",
        "mcp_servers": (
            HarnessMcpServer(
                name="agentbahn_memory",
                url="http://host.docker.internal:8001/mcp/projects/7/memory",
                headers=(HarnessMcpHeader(name="Authorization", value="Bearer tok"),),
            ),
        ),
    }
    fields.update(overrides)
    return HarnessConfiguration.model_validate(fields)


def test_settings_merge_mcp_servers_over_saved_settings() -> None:
    base = {
        "schema_version": 1,
        "model": "muse-spark-1.2",
        "mcp_servers": {"kept": {"transport": "stdio", "command": "keep-me"}},
    }

    merged = settings_with_mcp_servers(base, _mcp_config())

    assert merged["model"] == "muse-spark-1.2"
    assert merged["mcp_servers"] == {
        "kept": {"transport": "stdio", "command": "keep-me"},
        "agentbahn_memory": {
            "transport": "streamable_http",
            "url": "http://host.docker.internal:8001/mcp/projects/7/memory",
            "headers": {"Authorization": "Bearer tok"},
        },
    }
    assert settings_with_mcp_servers({}, _mcp_config())["schema_version"] == 1


def test_render_config_dir_links_credentials_and_writes_private_settings(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base" / "muse"
    base.mkdir(parents=True)
    (base / "settings.json").write_text(
        json.dumps({"schema_version": 1, "tui": {"theme": "dark"}}), encoding="utf-8"
    )
    (base / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")

    xdg_root = render_config_dir(_mcp_config(), base=base, root=tmp_path)

    muse_dir = xdg_root / "muse"
    settings = json.loads((muse_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["tui"] == {"theme": "dark"}
    assert "agentbahn_memory" in settings["mcp_servers"]
    assert (muse_dir / "auth.json").is_symlink()
    assert (muse_dir / "auth.json").resolve() == (base / "auth.json").resolve()
    assert not (muse_dir / "trust.json").exists()
    assert oct((muse_dir / "settings.json").stat().st_mode & 0o777) == "0o600"
    # Base settings are never touched.
    assert "mcp_servers" not in json.loads((base / "settings.json").read_text(encoding="utf-8"))

    remove_config_dir(xdg_root)
    assert not xdg_root.exists()
    assert (base / "auth.json").is_file()


async def test_adapter_environment_points_host_at_private_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home-config"))
    monkeypatch.setattr(config_dir_module.tempfile, "gettempdir", lambda: str(tmp_path))
    adapter = MuseAdapter()
    plain = HarnessConfiguration(kind=HarnessKind.MUSE, working_directory="/tmp")

    assert adapter.build_environment(plain) == {}

    environment = adapter.build_environment(_mcp_config())
    xdg_root = Path(environment["XDG_CONFIG_HOME"])
    assert xdg_root.parent == tmp_path
    assert xdg_root.name.startswith("tth-muse-config-")
    settings = json.loads((xdg_root / "muse" / "settings.json").read_text(encoding="utf-8"))
    assert settings["mcp_servers"]["agentbahn_memory"]["transport"] == "streamable_http"

    await adapter.close(
        HarnessSession(
            conversation_id=uuid4(),
            binding_id=uuid4(),
            kind=HarnessKind.MUSE,
            native_session_id="s",
        )
    )
    assert not xdg_root.exists()
