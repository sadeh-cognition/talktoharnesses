"""Django committed-event broker: SQLite in-process + optional PostgreSQL NOTIFY."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import connection

from talktoharnesses.application.broker import (
    KEEPALIVE_INTERVAL_S,
    SQLITE_POLL_INTERVAL_S,
    InProcessCommittedEventBroker,
)
from talktoharnesses.application.publisher import ConversationWakeup
from talktoharnesses.domain.events import ConversationEvent

logger = logging.getLogger(__name__)

_PG_CHANNEL = "talktoharnesses_events"


class DjangoCommittedEventBroker:
    """Wakeup broker used by ASGI processes.

    - Always fans out in-process (SQLite single-supervisor profile).
    - On PostgreSQL, also emits ``pg_notify`` after publish and listens on a
      dedicated autocommit connection so other processes can wake.
    """

    def __init__(
        self,
        *,
        poll_interval: float = SQLITE_POLL_INTERVAL_S,
        keepalive_interval: float = KEEPALIVE_INTERVAL_S,
    ) -> None:
        self._local = InProcessCommittedEventBroker(
            poll_interval=poll_interval,
            keepalive_interval=keepalive_interval,
        )
        self._started = False
        self._pg_thread: threading.Thread | None = None
        self._pg_stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        await self._local.start()
        if connection.vendor == "postgresql":
            self._pg_stop.clear()
            self._pg_thread = threading.Thread(
                target=self._pg_listen_loop,
                name="talktoharnesses-pg-notify",
                daemon=True,
            )
            self._pg_thread.start()

    async def stop(self) -> None:
        self._started = False
        self._pg_stop.set()
        if self._pg_thread is not None:
            self._pg_thread.join(timeout=2.0)
            self._pg_thread = None
        await self._local.stop()
        self._loop = None

    async def publish(self, events: Sequence[ConversationEvent]) -> None:
        if not events:
            return
        # In-process wakeups first (same process SSE subscribers).
        await self._local.publish(events)
        if connection.vendor != "postgresql":
            return
        # Notify other processes; payload is conversation UUID + high sequence only.
        by_conversation: dict[UUID, int] = {}
        for event in events:
            cid = event.conversation_id
            prev = by_conversation.get(cid, 0)
            if event.sequence > prev:
                by_conversation[cid] = event.sequence
        try:
            await sync_to_async(self._pg_notify, thread_sensitive=True)(by_conversation)
        except Exception:
            logger.exception("pg_notify failed; local wakeups already delivered")

    def subscribe(self, conversation_id: UUID) -> AsyncIterator[ConversationWakeup]:
        return self._local.subscribe(conversation_id)

    def high_water(self, conversation_id: UUID) -> int:
        return self._local.high_water(conversation_id)

    def _pg_notify(self, by_conversation: dict[UUID, int]) -> None:
        with connection.cursor() as cursor:
            for conversation_id, sequence in by_conversation.items():
                payload = f"{conversation_id}:{sequence}"
                # Use parameterized NOTIFY via pg_notify function.
                cursor.execute(
                    "SELECT pg_notify(%s, %s)",
                    [_PG_CHANNEL, payload],
                )

    def _pg_listen_loop(self) -> None:
        """Dedicated autocommit listener connection (psycopg3)."""
        try:
            import psycopg  # pyright: ignore[reportMissingImports]
            from django.db import connections
        except ImportError:
            logger.warning("psycopg not installed; skipping PostgreSQL LISTEN")
            return

        db_settings = connections["default"].settings_dict
        conninfo = _psycopg_conninfo(db_settings)
        try:
            # psycopg is an optional [postgres] extra; keep the loop loosely typed.
            with psycopg.connect(conninfo, autocommit=True) as conn:  # type: ignore[no-untyped-call]
                conn.execute(f"LISTEN {_PG_CHANNEL}")  # type: ignore[union-attr]
                while not self._pg_stop.is_set():
                    for notify in conn.notifies(timeout=1.0):  # type: ignore[union-attr]
                        self._handle_pg_notify(str(notify.payload))  # type: ignore[union-attr]
        except Exception:
            if not self._pg_stop.is_set():
                logger.exception("PostgreSQL LISTEN loop failed")

    def _handle_pg_notify(self, payload: str | None) -> None:
        if not payload or ":" not in payload:
            return
        try:
            cid_s, seq_s = payload.rsplit(":", 1)
            conversation_id = UUID(cid_s)
            sequence = int(seq_s)
        except (ValueError, TypeError):
            logger.warning("ignoring malformed pg_notify payload")
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        def _schedule() -> None:
            asyncio.create_task(self._local.notify(conversation_id, sequence))

        loop.call_soon_threadsafe(_schedule)


def _psycopg_conninfo(settings_dict: dict[str, Any]) -> str:
    """Build a psycopg3 conninfo string from Django DATABASES settings."""
    parts: list[str] = []
    mapping = {
        "dbname": settings_dict.get("NAME"),
        "user": settings_dict.get("USER"),
        "password": settings_dict.get("PASSWORD"),
        "host": settings_dict.get("HOST"),
        "port": settings_dict.get("PORT"),
    }
    for key, value in mapping.items():
        if value is not None and value != "":
            parts.append(f"{key}={value}")
    return " ".join(parts)
