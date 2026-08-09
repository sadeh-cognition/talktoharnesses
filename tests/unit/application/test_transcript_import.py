"""Transcript converters and facade export/import with fake runtime."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tests.runtime.conftest import FakeAdapter, SeedReply
from tests.runtime.memory_persistence import MemoryPersistence

from talktoharnesses.application.handoff import (
    HandoffDocument,
    HandoffMessage,
    HandoffTool,
    render_handoff,
)
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.application.transcripts import (
    handoff_to_transcript,
    redact_transcript,
    transcript_to_handoff,
)
from talktoharnesses.domain import (
    DomainError,
    ErrorCode,
    HarnessConfiguration,
    HarnessKind,
    MessageRole,
    ToolOutcome,
    TurnStatus,
    dump_transcript_document,
    load_transcript_document,
)
from talktoharnesses.domain.events import ConversationEvent
from talktoharnesses.domain.models import Message, Turn
from talktoharnesses.domain.transcripts import (
    TranscriptDocument,
    TranscriptMessage,
    TranscriptTool,
    TranscriptTurn,
)
from talktoharnesses.providers.registry import AdapterRegistry
from talktoharnesses.runtime.manager import RuntimeManager
from talktoharnesses.runtime.policy import RuntimePolicy


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


class _ImportAdapter(FakeAdapter):
    sdk_managed = True

    def __init__(self, kind: HarnessKind, *, seed_reply: SeedReply = "completed") -> None:
        super().__init__(seed_reply=seed_reply)
        self.kind = kind


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ConversationEvent] = []
        self.fail = False

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        if self.fail:
            raise RuntimeError("publisher failed")
        self.events.extend(events)


def _config(kind: HarnessKind, workdir: Path) -> HarnessConfiguration:
    return HarnessConfiguration(kind=kind, working_directory=str(workdir))


def _service(
    workdir: Path,
    *,
    seed_reply: SeedReply = "completed",
) -> tuple[TalkToHarnessesService, MemoryPersistence, _Publisher, list[_ImportAdapter]]:
    persistence = MemoryPersistence()
    adapters: list[_ImportAdapter] = []

    def factory(kind: HarnessKind) -> _ImportAdapter:
        adapter = _ImportAdapter(kind, seed_reply=seed_reply)
        adapters.append(adapter)
        return adapter

    registry = AdapterRegistry()
    registry.register(HarnessKind.GROK, lambda: factory(HarnessKind.GROK))  # type: ignore[arg-type]
    registry.register(HarnessKind.CODEX, lambda: factory(HarnessKind.CODEX))  # type: ignore[arg-type]
    publisher = _Publisher()
    runtime = RuntimeManager(
        persistence,
        registry,
        policy=RuntimePolicy(start_resume_timeout=2.0, graceful_close_timeout=0.3),
        clock=_now,
    )
    service = TalkToHarnessesService(persistence, registry, publisher, _now, runtime)
    return service, persistence, publisher, adapters


def _sample_document() -> TranscriptDocument:
    return TranscriptDocument(
        format="talktoharnesses.canonical-transcript",
        version=1,
        title="Portable",
        turns=(
            TranscriptTurn(
                entries=(
                    TranscriptMessage(role="user", text="build the thing"),
                    TranscriptMessage(role="assistant", text="done"),
                    TranscriptTool(
                        tool_name="shell",
                        arguments={"cmd": "make"},
                        outcome=ToolOutcome.SUCCESS,
                        output_tail="ok",
                    ),
                )
            ),
        ),
    )


def test_converter_round_trip_preserves_content() -> None:
    document = _sample_document()
    handoff = transcript_to_handoff(document)
    restored = handoff_to_transcript(handoff, document.title)
    assert restored.title == document.title
    assert len(restored.turns) == 1
    assert [entry.model_dump(mode="json") for entry in restored.turns[0].entries] == [
        entry.model_dump(mode="json") for entry in document.turns[0].entries
    ]
    # Prospective IDs must not reuse source identity (none in the document).
    assert {entry.id for entry in handoff.entries}
    assert len({entry.turn_id for entry in handoff.entries}) == 1


def test_handoff_grouping_preserves_turn_order() -> None:
    turn_a = uuid4()
    turn_b = uuid4()
    handoff = HandoffDocument(
        entries=(
            HandoffMessage(
                id=uuid4(),
                turn_id=turn_a,
                role=MessageRole.USER,
                text="one",
                turn_order_index=1,
                order_index=1,
            ),
            HandoffTool(
                id=uuid4(),
                turn_id=turn_a,
                tool_name="t",
                outcome=ToolOutcome.SUCCESS,
                turn_order_index=1,
                order_index=2,
            ),
            HandoffMessage(
                id=uuid4(),
                turn_id=turn_b,
                role=MessageRole.USER,
                text="two",
                turn_order_index=2,
                order_index=1,
            ),
        )
    )
    document = handoff_to_transcript(handoff, "T")
    assert [turn.entries[0].text for turn in document.turns] == ["one", "two"]  # type: ignore[union-attr]
    assert document.turns[0].entries[1].type == "tool"


def test_redaction_covers_all_nested_tool_strings() -> None:
    document = TranscriptDocument(
        format="talktoharnesses.canonical-transcript",
        version=1,
        title="SECRET title",
        turns=(
            TranscriptTurn(
                entries=(
                    TranscriptMessage(role="user", text="SECRET prompt"),
                    TranscriptTool(
                        tool_name="SECRET-tool",
                        arguments={
                            "SECRET-key": {"nested": ["SECRET-value", {"SECRET-inner": "clean"}]}
                        },
                        outcome=ToolOutcome.SUCCESS,
                        paths=("/SECRET/path",),
                        output_tail="SECRET output",
                    ),
                )
            ),
        ),
    )
    redacted = redact_transcript(document, ("SECRET",))
    assert "SECRET" not in dump_transcript_document(redacted)
    assert dump_transcript_document(redacted).count("[REDACTED]") >= 7


@pytest.mark.asyncio
async def test_export_from_retained_handoff(tmp_path: Path) -> None:
    service, persistence, _publisher, _adapters = _service(tmp_path)
    harness = await service.create_harness(
        "owner",
        name="a",
        configuration=_config(HarnessKind.GROK, tmp_path),
    )
    snapshot = await service.create_conversation("owner", harness.id, title="Manual Title")
    cid = snapshot.detail.conversation.id
    turn_id = uuid4()
    message_id = uuid4()
    persistence.turns[cid][turn_id] = Turn(
        id=turn_id,
        conversation_id=cid,
        status=TurnStatus.COMPLETED,
        user_message_id=message_id,
        created_at=_now(),
        completed_at=_now(),
    )
    persistence.turn_order[cid].append(turn_id)
    persistence.messages[cid][message_id] = Message(
        id=message_id,
        turn_id=turn_id,
        role=MessageRole.USER,
        text="build the thing",
        created_at=_now(),
    )
    persistence.item_order_index[cid] = {message_id: 1}

    document = await service.export_transcript("owner", cid)
    assert document.title == "Manual Title"
    assert document.turns[0].entries[0].text == "build the thing"  # type: ignore[union-attr]
    # Deterministic file form.
    assert dump_transcript_document(document) == dump_transcript_document(
        load_transcript_document(dump_transcript_document(document))
    )


@pytest.mark.asyncio
async def test_import_seeds_candidate_then_commits(tmp_path: Path) -> None:
    service, persistence, publisher, adapters = _service(tmp_path)
    harness = await service.create_harness(
        "owner",
        name="a",
        configuration=_config(HarnessKind.GROK, tmp_path),
    )
    document = _sample_document()
    created = len(adapters)

    snapshot = await service.import_transcript("owner", harness.id, document)

    assert snapshot.detail.conversation.title_manual == "Portable"
    assert snapshot.detail.conversation.id not in {UUID(int=0)}
    imported_id = snapshot.detail.conversation.id
    handoff = await persistence.read_retained_handoff(imported_id, owner_id="owner")
    assert [render_handoff(handoff)] == [
        "[user]: build the thing\n[assistant]: done\n"
        '[tool:shell] outcome=success arguments={"cmd": "make"} output=ok'
    ]
    assert any(event.type == "transcript_imported" for event in publisher.events)
    (adapter,) = adapters[created:]
    assert adapter.submissions
    assert "[user]: build the thing" in adapter.submissions[0].prompt
    promoted = service._runtime.get_runtime(imported_id)  # pyright: ignore[reportPrivateUsage]
    assert promoted is not None
    await service.stop()


@pytest.mark.asyncio
async def test_import_closes_candidate_when_seed_fails(tmp_path: Path) -> None:
    service, persistence, publisher, adapters = _service(tmp_path, seed_reply="failed")
    harness = await service.create_harness(
        "owner",
        name="a",
        configuration=_config(HarnessKind.GROK, tmp_path),
    )
    created = len(adapters)
    with pytest.raises(DomainError) as exc:
        await service.import_transcript("owner", harness.id, _sample_document())
    assert exc.value.code is ErrorCode.PROTOCOL_ERROR
    assert persistence.states == {}
    assert publisher.events == []
    (adapter,) = adapters[created:]
    assert adapter.closed is True
    await service.stop()


@pytest.mark.asyncio
async def test_import_promotes_candidate_when_publication_fails(tmp_path: Path) -> None:
    service, persistence, publisher, _adapters = _service(tmp_path)
    harness = await service.create_harness(
        "owner",
        name="a",
        configuration=_config(HarnessKind.GROK, tmp_path),
    )
    publisher.fail = True

    snapshot = await service.import_transcript("owner", harness.id, _sample_document())

    conversation_id = snapshot.detail.conversation.id
    assert conversation_id in persistence.states
    assert service._runtime.get_runtime(conversation_id) is not None  # pyright: ignore[reportPrivateUsage]
    binding = persistence.states[conversation_id].binding
    assert binding is not None
    assert service._runtime.get_candidate(binding.id) is None  # pyright: ignore[reportPrivateUsage]
    await service.stop()


@pytest.mark.asyncio
async def test_import_export_equivalence(tmp_path: Path) -> None:
    service, _persistence, _publisher, _adapters = _service(tmp_path)
    harness = await service.create_harness(
        "owner",
        name="a",
        configuration=_config(HarnessKind.GROK, tmp_path),
    )
    original = _sample_document()
    snapshot = await service.import_transcript("owner", harness.id, original)
    exported = await service.export_transcript("owner", snapshot.detail.conversation.id)
    assert dump_transcript_document(exported) == dump_transcript_document(original)
    await service.stop()
