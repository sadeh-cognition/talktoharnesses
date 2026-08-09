"""Django event broker wiring (SQLite path in CI)."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from talktoharnesses.django.broker import DjangoCommittedEventBroker
from talktoharnesses.domain.events import ConversationEvent, TurnStartedPayload


def _event(conversation_id: UUID, sequence: int) -> ConversationEvent:
    return ConversationEvent(
        conversation_id=conversation_id,
        sequence=sequence,
        timestamp=datetime(2026, 8, 8, tzinfo=UTC),
        type="turn_started",
        payload=TurnStartedPayload(turn_id=uuid4()),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_django_broker_sqlite_publish_and_subscribe() -> None:
    broker = DjangoCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    await broker.start()
    cid = uuid4()
    got: list[int] = []

    async def consume() -> None:
        async for w in broker.subscribe(cid):
            got.append(w.sequence)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await broker.publish([_event(cid, 2), _event(cid, 9)])
    await asyncio.wait_for(task, timeout=2.0)
    assert got == [9]
    assert broker.high_water(cid) == 9
    await broker.stop()
    await broker.stop()


@pytest.mark.asyncio
async def test_postgres_notify_runs_off_the_event_loop_thread() -> None:
    broker = DjangoCommittedEventBroker()
    conversation_id = uuid4()
    event_loop_thread = threading.get_ident()
    notify_threads: list[int] = []

    def notify(by_conversation: dict[UUID, int]) -> None:
        assert by_conversation == {conversation_id: 1}
        notify_threads.append(threading.get_ident())

    with (
        patch("talktoharnesses.django.broker.connection") as db_connection,
        patch.object(broker, "_pg_notify", side_effect=notify),
    ):
        db_connection.vendor = "postgresql"
        await broker.publish([_event(conversation_id, 1)])

    assert notify_threads
    assert notify_threads[0] != event_loop_thread


def test_psycopg_conninfo_builds_from_django_settings() -> None:
    from talktoharnesses.django.broker import (
        _psycopg_conninfo,  # pyright: ignore[reportPrivateUsage]
    )

    conninfo = _psycopg_conninfo(
        {
            "NAME": "talkto",
            "USER": "u",
            "PASSWORD": "p",
            "HOST": "localhost",
            "PORT": "5432",
            "OPTIONS": {},
        }
    )
    assert "dbname=talkto" in conninfo
    assert "user=u" in conninfo
    assert "password=p" in conninfo
    assert "host=localhost" in conninfo
    assert "port=5432" in conninfo
    assert _psycopg_conninfo({"NAME": "", "USER": None}) == ""  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_handle_pg_notify_schedules_local_wakeup() -> None:
    broker = DjangoCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    await broker.start()
    cid = uuid4()
    got: list[int] = []

    async def consume() -> None:
        async for w in broker.subscribe(cid):
            got.append(w.sequence)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    broker._handle_pg_notify(f"{cid}:7")  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(task, timeout=2.0)
    assert got == [7]
    # Malformed payloads are ignored.
    broker._handle_pg_notify(None)  # pyright: ignore[reportPrivateUsage]
    broker._handle_pg_notify("not-a-uuid")  # pyright: ignore[reportPrivateUsage]
    broker._handle_pg_notify("bad:seq")  # pyright: ignore[reportPrivateUsage]
    await broker.stop()


@pytest.mark.asyncio
async def test_start_stop_postgresql_vendor_branch() -> None:
    broker = DjangoCommittedEventBroker(poll_interval=10.0, keepalive_interval=30.0)
    started = threading.Event()

    def fake_listen() -> None:
        started.set()
        broker._pg_stop.wait(timeout=2.0)  # pyright: ignore[reportPrivateUsage]

    with (
        patch("talktoharnesses.django.broker.connection") as db_connection,
        patch.object(broker, "_pg_listen_loop", side_effect=fake_listen),
    ):
        db_connection.vendor = "postgresql"
        await broker.start()
        assert started.wait(timeout=1.0)
        assert broker._pg_thread is not None  # pyright: ignore[reportPrivateUsage]
        await broker.stop()
        assert broker._pg_thread is None  # pyright: ignore[reportPrivateUsage]
        # Idempotent stop.
        await broker.stop()


def test_pg_notify_executes_parameterized_notify() -> None:
    broker = DjangoCommittedEventBroker()
    executed: list[tuple[str, list[object]]] = []

    class _Cursor:
        def execute(self, sql: str, params: list[object]) -> None:
            executed.append((sql, params))

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

    with patch("talktoharnesses.django.broker.connection", _Conn()):
        cid = uuid4()
        broker._pg_notify({cid: 9})  # pyright: ignore[reportPrivateUsage]
    assert executed
    assert "pg_notify" in executed[0][0]
    assert executed[0][1][1] == f"{cid}:9"


def test_pg_listen_loop_handles_notify_and_import_error() -> None:
    import sys
    import types

    broker = DjangoCommittedEventBroker()
    broker._loop = None  # pyright: ignore[reportPrivateUsage]
    broker._pg_stop.set()  # pyright: ignore[reportPrivateUsage]

    # Missing psycopg exits cleanly.
    with patch.dict(sys.modules, {"psycopg": None}):
        # ImportError path: make import raise.
        import builtins

        real_import = builtins.__import__

        def _import(name: str, *args: object, **kwargs: object):  # noqa: ANN001
            if name == "psycopg":
                raise ImportError("no psycopg")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        with patch("builtins.__import__", side_effect=_import):
            broker._pg_listen_loop()  # pyright: ignore[reportPrivateUsage]

    class _Notify:
        payload = f"{uuid4()}:3"

    class _Conn:
        def __init__(self) -> None:
            self._calls = 0

        def execute(self, *_a: object, **_k: object) -> None:
            return None

        def notifies(self, timeout: float = 1.0) -> list[_Notify]:
            del timeout
            self._calls += 1
            if self._calls == 1:
                return [_Notify()]
            broker._pg_stop.set()  # pyright: ignore[reportPrivateUsage]
            return []

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    broker._pg_stop.clear()  # pyright: ignore[reportPrivateUsage]
    handled: list[str | None] = []

    def capture(payload: str | None) -> None:
        handled.append(payload)
        broker._pg_stop.set()  # pyright: ignore[reportPrivateUsage]

    fake = types.ModuleType("psycopg")

    def _connect(*_a: object, **_k: object) -> _Conn:
        return _Conn()

    fake.connect = _connect  # type: ignore[attr-defined]
    with (
        patch.dict(sys.modules, {"psycopg": fake}),
        patch("django.db.connections") as connections,
        patch.object(broker, "_handle_pg_notify", side_effect=capture),
    ):
        connections.__getitem__.return_value.settings_dict = {
            "NAME": "db",
            "USER": "u",
            "PASSWORD": "",
            "HOST": "localhost",
            "PORT": "5432",
        }
        broker._pg_listen_loop()  # pyright: ignore[reportPrivateUsage]
    assert handled
