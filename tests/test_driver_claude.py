"""Claude driver tests with an injected fake ClaudeSDKClient."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from talktoharnesses.drivers.claude import ClaudeHarness
from talktoharnesses.events import ContentDelta, RequestOpened, RequestResolved


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeAssistantMessage:
    content: list[Any]
    model: str = "claude-test"
    message_id: str | None = "msg-1"
    parent_tool_use_id: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    stop_reason: str | None = "end_turn"
    session_id: str | None = "sess-1"
    uuid: str | None = None


@dataclass
class FakeStreamEvent:
    """Partial message, as yielded when include_partial_messages=True."""

    event: dict[str, Any]
    session_id: str = "sess-1"
    uuid: str | None = None
    parent_tool_use_id: str | None = None


def text_delta(text: str) -> FakeStreamEvent:
    return FakeStreamEvent(
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    )


def thinking_delta(text: str) -> FakeStreamEvent:
    return FakeStreamEvent(
        event={
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text},
        }
    )


def json_delta(fragment: str) -> FakeStreamEvent:
    """Streamed *tool arguments* — never assistant-visible prose."""
    return FakeStreamEvent(
        event={
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": fragment},
        }
    )


@dataclass
class FakeThinkingBlock:
    thinking: str


@dataclass
class FakeResultMessage:
    subtype: str = "success"
    duration_ms: int = 1
    duration_api_ms: int = 1
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "sess-1"
    stop_reason: str | None = "end_turn"
    total_cost_usd: float | None = 0.0
    usage: dict[str, Any] | None = None
    result: str | None = "OK"
    structured_output: Any = None
    model_usage: dict[str, Any] | None = None
    permission_denials: list[Any] | None = None
    deferred_tool_use: Any = None
    errors: list[str] | None = None
    api_error_status: int | None = None
    uuid: str | None = None
    terminal_reason: str | None = None


class FakeClaudeClient:
    """Minimal stand-in for ClaudeSDKClient."""

    def __init__(
        self,
        options: Any = None,
        *,
        messages: list[Any] | None = None,
        permission_tool: str | None = None,
    ) -> None:
        self.options = options
        self._messages = messages or [
            FakeAssistantMessage(content=[FakeTextBlock("Hel"), FakeTextBlock("lo OK")]),
            FakeResultMessage(),
        ]
        self._permission_tool = permission_tool
        self.connected = False
        self.queries: list[str] = []
        self.can_use_tool = getattr(options, "can_use_tool", None)

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)
        # Optionally exercise can_use_tool before yielding messages.
        if self._permission_tool and self.can_use_tool is not None:
            await self.can_use_tool(self._permission_tool, {"path": "/tmp"}, None)

    async def receive_response(self) -> AsyncIterator[Any]:
        for m in self._messages:
            yield m

    async def interrupt(self) -> None:
        return None

    async def set_model(self, model: str | None = None) -> None:
        return None


async def test_claude_turn_with_fake_client(tmp_path: Path) -> None:
    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(options)

    h = ClaudeHarness(cwd=tmp_path, model="claude-test", client_factory=factory)
    try:
        session = await h.start_session()
        assert session.provider == "claude"
        events = [ev async for ev in h.send_turn("hi")]
        types = [e.type for e in events]
        assert "turn.started" in types
        assert "content.delta" in types
        assert "turn.completed" in types
        text = "".join(
            e.text for e in events if isinstance(e, ContentDelta) and e.content_kind == "text"
        )
        assert "OK" in text
    finally:
        await h.aclose()


async def test_claude_can_use_tool_bridge(tmp_path: Path) -> None:
    decisions: list[Any] = []

    def factory(options: Any) -> FakeClaudeClient:
        client = FakeClaudeClient(
            options,
            messages=[
                FakeAssistantMessage(content=[FakeTextBlock("done")]),
                FakeResultMessage(),
            ],
            permission_tool="Bash",
        )
        return client

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        opened: RequestOpened | None = None
        resolved: RequestResolved | None = None
        async for ev in h.send_turn("run bash"):
            if isinstance(ev, RequestOpened) and opened is None:
                opened = ev
                await h.respond(ev.request_id or "", "accept")
            if isinstance(ev, RequestResolved):
                resolved = ev
                decisions.append(ev.decision)
        assert opened is not None
        assert opened.tool_name == "Bash"
        assert resolved is not None
        assert resolved.decision in ("allow", "allow_for_session")
    finally:
        await h.aclose()


async def test_claude_decline_tool(tmp_path: Path) -> None:
    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[FakeAssistantMessage(content=[FakeTextBlock("nope")]), FakeResultMessage()],
            permission_tool="Write",
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        resolved: RequestResolved | None = None
        async for ev in h.send_turn("write file"):
            if isinstance(ev, RequestOpened):
                await h.respond(ev.request_id or "", "decline")
            if isinstance(ev, RequestResolved):
                resolved = ev
        assert resolved is not None
        assert resolved.decision == "deny"
    finally:
        await h.aclose()


# ---------------------------------------------------------------------------
# Regression: partial StreamEvents + assembled AssistantMessage must not
# both produce content.delta. include_partial_messages is always on, so the
# real SDK yields both for every turn.
# ---------------------------------------------------------------------------


def _text(events: list[Any], kind: str = "text") -> str:
    return "".join(
        e.text for e in events if isinstance(e, ContentDelta) and e.content_kind == kind
    )


async def test_streamed_text_is_not_duplicated_by_assistant_message(tmp_path: Path) -> None:
    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[
                text_delta("Hel"),
                text_delta("lo"),
                # The SDK then delivers the assembled message with the SAME text.
                FakeAssistantMessage(content=[FakeTextBlock("Hello")]),
                FakeResultMessage(),
            ],
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("hi")]
        assert _text(events) == "Hello"
    finally:
        await h.aclose()


async def test_streamed_thinking_is_not_duplicated(tmp_path: Path) -> None:
    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[
                thinking_delta("we"),
                thinking_delta("igh"),
                FakeAssistantMessage(content=[FakeThinkingBlock("weigh"), FakeTextBlock("hi")]),
                FakeResultMessage(),
            ],
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("think")]
        assert _text(events, "reasoning") == "weigh"
        # Text never streamed, so the assembled block is the only source.
        assert _text(events, "text") == "hi"
    finally:
        await h.aclose()


async def test_assistant_message_is_the_fallback_when_no_partials(tmp_path: Path) -> None:
    """A turn that streams nothing must still produce its text exactly once."""

    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[
                FakeAssistantMessage(content=[FakeTextBlock("only")]),
                FakeResultMessage(),
            ],
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("hi")]
        assert _text(events) == "only"
    finally:
        await h.aclose()


async def test_tool_argument_json_is_not_emitted_as_text(tmp_path: Path) -> None:
    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[
                text_delta("running "),
                json_delta('{"path":'),
                json_delta('"/tmp"}'),
                FakeAssistantMessage(content=[FakeToolUseBlock(id="t1", name="Read")]),
                FakeResultMessage(),
            ],
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("read it")]
        assert _text(events) == "running "
    finally:
        await h.aclose()


async def test_streamed_kinds_do_not_leak_across_turns(tmp_path: Path) -> None:
    """Suppression is per-turn: a later non-streaming turn still emits text."""

    class TwoTurnClient(FakeClaudeClient):
        def __init__(self, options: Any = None) -> None:
            super().__init__(options)
            self._turn = 0

        async def receive_response(self) -> AsyncIterator[Any]:
            self._turn += 1
            if self._turn == 1:
                yield text_delta("first")
                yield FakeAssistantMessage(content=[FakeTextBlock("first")])
            else:
                yield FakeAssistantMessage(content=[FakeTextBlock("second")])
            yield FakeResultMessage()

    h = ClaudeHarness(cwd=tmp_path, client_factory=lambda o: TwoTurnClient(o))
    try:
        await h.start_session()
        first = [ev async for ev in h.send_turn("a")]
        second = [ev async for ev in h.send_turn("b")]
        assert _text(first) == "first"
        assert _text(second) == "second"
    finally:
        await h.aclose()


# ---------------------------------------------------------------------------
# Tool-call lifecycle (#5) and session-scoped approvals (#7)
# ---------------------------------------------------------------------------


@dataclass
class FakeToolResultBlock:
    tool_use_id: str
    content: Any = "ok"
    is_error: bool = False


@dataclass
class FakeUserMessage:
    content: list[Any]
    session_id: str = "sess-1"


async def test_tool_call_completes_only_when_its_result_arrives(tmp_path: Path) -> None:
    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[
                FakeAssistantMessage(content=[FakeToolUseBlock(id="t1", name="Read")]),
                FakeUserMessage(content=[FakeToolResultBlock(tool_use_id="t1", content="data")]),
                FakeAssistantMessage(content=[FakeTextBlock("done")]),
                FakeResultMessage(),
            ],
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("read")]
        order = [e.type for e in events if e.type in ("item.started", "item.completed")]
        assert order == ["item.started", "item.completed"]

        started = next(e for e in events if e.type == "item.started")
        completed = next(e for e in events if e.type == "item.completed")
        assert started.item_id == "t1"
        assert completed.item_id == "t1"
        # The old behaviour reported completion immediately, with status "started".
        assert completed.status == "completed"
    finally:
        await h.aclose()


async def test_failed_tool_result_is_marked_error(tmp_path: Path) -> None:
    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[
                FakeAssistantMessage(content=[FakeToolUseBlock(id="t1", name="Bash")]),
                FakeUserMessage(
                    content=[FakeToolResultBlock(tool_use_id="t1", content="boom", is_error=True)]
                ),
                FakeResultMessage(),
            ],
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("run")]
        completed = next(e for e in events if e.type == "item.completed")
        assert completed.status == "error"
        assert completed.detail == "boom"
    finally:
        await h.aclose()


async def test_tool_call_without_result_is_closed_at_turn_end(tmp_path: Path) -> None:
    """A dangling item.started would otherwise never be resolved."""

    def factory(options: Any) -> FakeClaudeClient:
        return FakeClaudeClient(
            options,
            messages=[
                FakeAssistantMessage(content=[FakeToolUseBlock(id="t9", name="Read")]),
                FakeResultMessage(),
            ],
        )

    h = ClaudeHarness(cwd=tmp_path, client_factory=factory)
    try:
        await h.start_session()
        events = [ev async for ev in h.send_turn("read")]
        completed = [e for e in events if e.type == "item.completed"]
        assert [e.item_id for e in completed] == ["t9"]
    finally:
        await h.aclose()


async def test_accept_for_session_is_not_re_prompted(tmp_path: Path) -> None:
    class TwicePrompting(FakeClaudeClient):
        async def query(self, prompt: str, session_id: str = "default") -> None:
            assert self.can_use_tool is not None
            await self.can_use_tool("Bash", {"cmd": "ls"}, None)
            await self.can_use_tool("Bash", {"cmd": "pwd"}, None)

    h = ClaudeHarness(cwd=tmp_path, client_factory=lambda o: TwicePrompting(o))
    try:
        await h.start_session()
        opened = 0
        async for ev in h.send_turn("twice"):
            if isinstance(ev, RequestOpened):
                opened += 1
                await h.respond(ev.request_id or "", "accept_for_session")
        # Second call must be auto-allowed rather than prompting again.
        assert opened == 1
    finally:
        await h.aclose()


async def test_plain_accept_still_prompts_each_time(tmp_path: Path) -> None:
    class TwicePrompting(FakeClaudeClient):
        async def query(self, prompt: str, session_id: str = "default") -> None:
            assert self.can_use_tool is not None
            await self.can_use_tool("Bash", {"cmd": "ls"}, None)
            await self.can_use_tool("Bash", {"cmd": "pwd"}, None)

    h = ClaudeHarness(cwd=tmp_path, client_factory=lambda o: TwicePrompting(o))
    try:
        await h.start_session()
        opened = 0
        async for ev in h.send_turn("twice"):
            if isinstance(ev, RequestOpened):
                opened += 1
                await h.respond(ev.request_id or "", "accept")
        assert opened == 2
    finally:
        await h.aclose()
