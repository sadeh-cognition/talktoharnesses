"""Registry and harness() factory tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from talktoharnesses import harness
from talktoharnesses.errors import UnknownHarnessError
from talktoharnesses.events import RuntimeEvent, TurnCompleted, TurnStarted
from talktoharnesses.registry import (
    KNOWN_HARNESS_NAMES,
    create_harness,
    get_factory,
    register,
    registered_names,
    unregister,
)
from talktoharnesses.types import (
    ApprovalDecision,
    Capabilities,
    SendTurnInput,
    Session,
    SessionStartInput,
)


class FakeHarness:
    """Minimal Harness used to exercise the factory without real drivers."""

    name = "fake"
    capabilities = Capabilities()

    def __init__(self, *, cwd: Path, **config: Any) -> None:
        self.cwd = cwd
        self.config = config
        self.closed = False
        self._session: Session | None = None

    async def start_session(self, input: SessionStartInput | None = None) -> Session:
        self._session = Session(
            session_id="fake-session",
            thread_id="fake-thread",
            provider=self.name,
            model=(input.model if input else None) or self.config.get("model"),
            started_at=datetime.now(UTC),
        )
        return self._session

    async def send_turn(self, prompt: str | SendTurnInput) -> AsyncIterator[RuntimeEvent]:
        text = prompt if isinstance(prompt, str) else prompt.prompt
        yield TurnStarted(provider=self.name, turn_id="t1", thread_id="fake-thread")
        yield TurnCompleted(
            provider=self.name,
            turn_id="t1",
            thread_id="fake-thread",
            stop_reason=f"echo:{text}",
        )

    async def stream_events(self) -> AsyncIterator[RuntimeEvent]:
        return
        yield  # pragma: no cover — makes this an async generator

    async def interrupt_turn(self, turn_id: str | None = None) -> None:
        return None

    async def respond(self, request_id: str, decision: ApprovalDecision) -> None:
        return None

    async def respond_to_user_input(
        self,
        request_id: str,
        answers: Mapping[str, Any],
    ) -> None:
        return None

    async def stop_session(self) -> None:
        self._session = None

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_registration() -> Any:
    register("fake", FakeHarness)
    yield
    unregister("fake")


def test_known_harness_names() -> None:
    assert set(KNOWN_HARNESS_NAMES) == {"claude", "codex", "cursor", "grok", "opencode"}


def test_register_and_lookup() -> None:
    assert "fake" in registered_names()
    factory = get_factory("fake")
    h = factory(cwd=Path("."))
    assert isinstance(h, FakeHarness)


def test_unknown_harness_raises() -> None:
    with pytest.raises(UnknownHarnessError) as exc:
        create_harness("does-not-exist")
    assert exc.value.name == "does-not-exist"
    assert "fake" in exc.value.available


def test_create_harness_passes_cwd_and_config(tmp_path: Path) -> None:
    h = create_harness("fake", cwd=tmp_path, model="m1", extra_flag=True)
    assert isinstance(h, FakeHarness)
    assert h.cwd == tmp_path
    assert h.config["model"] == "m1"
    assert h.config["extra_flag"] is True


async def test_harness_context_manager_closes(tmp_path: Path) -> None:
    async with harness("fake", cwd=tmp_path, model="x") as h:
        assert isinstance(h, FakeHarness)
        assert h.cwd == tmp_path.resolve()
        session = await h.start_session()
        assert session.session_id == "fake-session"
        events = [ev async for ev in h.send_turn("hi")]
        assert [type(e).__name__ for e in events] == ["TurnStarted", "TurnCompleted"]
        assert not h.closed
    assert h.closed


async def test_harness_context_manager_closes_on_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with harness("fake", cwd=tmp_path) as h:
            assert isinstance(h, FakeHarness)
            raise RuntimeError("boom")
    assert h.closed


async def test_harness_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(UnknownHarnessError):
        async with harness("nope", cwd=tmp_path):
            pass  # pragma: no cover
