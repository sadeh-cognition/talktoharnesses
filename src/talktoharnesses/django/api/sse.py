"""Replay-safe SSE stream for conversation events."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from talktoharnesses.application.publisher import ConversationWakeup
from talktoharnesses.application.service import TalkToHarnessesService
from talktoharnesses.domain.enums import ErrorCode
from talktoharnesses.domain.errors import DomainError
from talktoharnesses.domain.events import ConversationEvent, ConversationMetadataChangedPayload
from talktoharnesses.domain.models import ConversationSnapshot, SyncProjection

logger = logging.getLogger(__name__)

_EVENT_COUNT_LIMIT = 5000
_BYTE_LIMIT = 5 * 1024 * 1024
_KEEPALIVE_S = 15.0


def parse_last_event_id(value: str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        seq = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(ErrorCode.INVALID_STATE, "invalid Last-Event-ID") from exc
    if seq < 0:
        raise DomainError(ErrorCode.INVALID_STATE, "invalid Last-Event-ID")
    return seq


def _frame(*, event: str, data: str, event_id: int | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    for part in data.split("\n"):
        lines.append(f"data: {part}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _event_frame(event: ConversationEvent) -> str:
    return _frame(event=event.type, data=event.model_dump_json(), event_id=event.sequence)


def _snapshot_frame(snapshot: ConversationSnapshot) -> str:
    return _frame(
        event="snapshot",
        data=snapshot.model_dump_json(),
        event_id=snapshot.sequence,
    )


def _sync_frame(sequence: int) -> str:
    return _frame(
        event="sync",
        data=SyncProjection(sequence=sequence).model_dump_json(),
        event_id=sequence,
    )


def _keepalive_frame() -> str:
    return ": keepalive\n\n"


def _byte_size(event: ConversationEvent) -> int:
    return len(event.model_dump_json().encode("utf-8"))


def _exceeds_caps(events: list[ConversationEvent], *, after: int, high_water: int) -> bool:
    if len(events) > _EVENT_COUNT_LIMIT:
        return True
    total = 0
    for event in events:
        size = _byte_size(event)
        if size > _BYTE_LIMIT or total + size > _BYTE_LIMIT:
            return True
        total += size
    replayed_through = events[-1].sequence if events else after
    return replayed_through < high_water


async def _bounded_replay(
    service: TalkToHarnessesService,
    *,
    owner_id: str,
    conversation_id: UUID,
    after: int,
) -> tuple[list[str], int, bool]:
    """Return frames and new last_sent after replay-or-snapshot (no sync)."""
    high_water = await service.get_stream_high_water_sequence(owner_id, conversation_id)
    if after <= high_water:
        events = list(
            await service.replay_stream_events(
                owner_id,
                conversation_id,
                after_sequence=after,
                event_count_limit=_EVENT_COUNT_LIMIT + 1,
                byte_limit=_BYTE_LIMIT + 1,
            )
        )
        if not _exceeds_caps(events, after=after, high_water=high_water):
            frames = [_event_frame(e) for e in events]
            last_sent = events[-1].sequence if events else after
            deleted = any(
                isinstance(event.payload, ConversationMetadataChangedPayload)
                and event.payload.deleted_at is not None
                for event in events
            )
            return frames, last_sent, deleted

    snapshot = await service.get_stream_snapshot(owner_id, conversation_id)
    deleted = snapshot.detail.conversation.deleted_at is not None
    return [_snapshot_frame(snapshot)], snapshot.sequence, deleted


async def iter_sse(
    service: TalkToHarnessesService,
    *,
    owner_id: str,
    conversation_id: UUID,
    last_event_id: int,
) -> AsyncIterator[str]:
    """Yield SSE frames: replay|snapshot → sync → live (until cancelled)."""
    publisher = service.publisher
    subscribe = getattr(publisher, "subscribe", None)
    if not callable(subscribe):
        raise DomainError(ErrorCode.INVALID_STATE, "event broker is not available")

    from typing import cast

    subscription = cast(AsyncIterator[ConversationWakeup], subscribe(conversation_id))
    last_sent = last_event_id
    try:
        # Open subscription before reading replay state (close race).
        aiter = subscription.__aiter__()

        frames, last_sent, deleted = await _bounded_replay(
            service,
            owner_id=owner_id,
            conversation_id=conversation_id,
            after=last_event_id,
        )
        for frame in frames:
            yield frame
        if deleted:
            return
        yield _sync_frame(last_sent)

        while True:
            try:
                await asyncio.wait_for(aiter.__anext__(), timeout=_KEEPALIVE_S)
            except TimeoutError:
                yield _keepalive_frame()
                frames, last_sent, deleted = await _bounded_replay(
                    service,
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    after=last_sent,
                )
                for frame in frames:
                    yield frame
                if deleted:
                    return
                continue
            except StopAsyncIteration:
                break

            frames, last_sent, deleted = await _bounded_replay(
                service,
                owner_id=owner_id,
                conversation_id=conversation_id,
                after=last_sent,
            )
            for frame in frames:
                yield frame
            if deleted:
                return
    except asyncio.CancelledError:
        raise
    finally:
        aclose = getattr(subscription, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(Exception):
                await cast_awaitable(aclose())


async def cast_awaitable(value: object) -> None:
    from collections.abc import Awaitable
    from typing import cast

    if value is not None:
        await cast(Awaitable[object], value)
