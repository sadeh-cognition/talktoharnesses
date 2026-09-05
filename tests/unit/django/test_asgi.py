"""ASGI lifespan wrapper and process-local service accessor."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from talktoharnesses.django import asgi as asgi_mod
from talktoharnesses.django.asgi import (
    Message,
    Receive,
    Scope,
    Send,
    get_service,
    reset_service_for_tests,
    talktoharnesses_lifespan,
)
from talktoharnesses.domain import DomainError, ErrorCode, HarnessKind


@pytest.fixture(autouse=True)
def clear_service() -> Iterator[None]:
    reset_service_for_tests()
    yield
    reset_service_for_tests()


@pytest.fixture(autouse=True)
def docker_cli_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must pass on Docker-less machines; stub the startup guard."""
    monkeypatch.setattr(asgi_mod, "ensure_docker_cli_available", lambda: "/usr/bin/docker")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_lifespan_startup_and_shutdown() -> None:
    calls: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("inner")

    app = talktoharnesses_lifespan(inner)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    await queue.put({"type": "lifespan.startup"})
    await queue.put({"type": "lifespan.shutdown"})

    sent: list[Message] = []

    async def receive() -> Message:
        return await queue.get()

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    # After shutdown the accessor fails closed.
    with pytest.raises(DomainError):
        get_service()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_get_service_available_after_startup() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    app = talktoharnesses_lifespan(inner)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    await queue.put({"type": "lifespan.startup"})
    # Hold shutdown until we assert the accessor.
    shutdown_gate = asyncio.Event()

    async def receive() -> Message:
        msg = await queue.get()
        if msg["type"] == "lifespan.shutdown":
            await shutdown_gate.wait()
        return msg

    sent: list[str] = []

    async def send(message: Message) -> None:
        sent.append(message["type"])
        if message["type"] == "lifespan.startup.complete":
            service = get_service()
            assert service is not None
            await queue.put({"type": "lifespan.shutdown"})
            shutdown_gate.set()

    await app({"type": "lifespan"}, receive, send)
    assert "lifespan.startup.complete" in sent
    assert "lifespan.shutdown.complete" in sent


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_lifespan_startup_failure_reports_failed() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    app = talktoharnesses_lifespan(inner)
    queue: asyncio.Queue[Message] = asyncio.Queue()
    await queue.put({"type": "lifespan.startup"})
    sent: list[Message] = []

    async def receive() -> Message:
        return await queue.get()

    async def send(message: Message) -> None:
        sent.append(message)

    with patch.object(
        asgi_mod,
        "_startup",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await app({"type": "lifespan"}, receive, send)
    assert sent[0]["type"] == "lifespan.startup.failed"
    assert sent[0]["message"] == "startup failed"
    assert "boom" not in sent[0]["message"]
    with pytest.raises(DomainError):
        get_service()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_http_scope_passes_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    app = talktoharnesses_lifespan(inner)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        return None

    await app({"type": "http", "method": "GET", "path": "/"}, receive, send)
    assert seen == ["http"]


def test_get_service_fails_closed_without_lifespan() -> None:
    with pytest.raises(DomainError) as exc:
        get_service()
    assert "not started" in exc.value.message


def test_default_registry_contains_all_phase7_adapters() -> None:
    service = asgi_mod._build_service()  # pyright: ignore[reportPrivateUsage]
    kinds = service._registry.kinds()  # pyright: ignore[reportPrivateUsage]
    assert kinds == frozenset(
        {
            HarnessKind.GROK,
            HarnessKind.CURSOR,
            HarnessKind.CODEX,
            HarnessKind.CLAUDE,
            HarnessKind.OPENCODE,
            HarnessKind.PRIME_AGENT,
            HarnessKind.MUSE,
        }
    )


def test_build_service_fails_closed_without_docker_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from talktoharnesses.remote.sandbox import ensure_docker_cli_available

    def no_which(cmd: str) -> None:
        return None

    # Undo the autouse stub so the real guard runs against a missing CLI.
    monkeypatch.setattr(asgi_mod, "ensure_docker_cli_available", ensure_docker_cli_available)
    monkeypatch.setattr(shutil, "which", no_which)
    with pytest.raises(DomainError) as excinfo:
        asgi_mod._build_service()  # pyright: ignore[reportPrivateUsage]
    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert excinfo.value.details["reason"] == "docker_unavailable"


def test_appconfig_ready_starts_nothing() -> None:
    from django.apps import apps

    config = apps.get_app_config("talktoharnesses")
    # ready() is a no-op for workers; calling again must not start a service.
    config.ready()
    with pytest.raises(DomainError):
        get_service()


def test_build_service_applies_runtime_policy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTH_RUNTIME_IDLE_REAP_SECONDS", "42")
    monkeypatch.setenv("TTH_RUNTIME_MAX_RUNTIMES", "3")

    service = asgi_mod._build_service()  # pyright: ignore[reportPrivateUsage]

    policy = service._runtime._policy  # pyright: ignore[reportPrivateUsage]
    assert policy.idle_reap == 42.0
    assert policy.max_runtimes == 3


def test_build_service_fails_closed_on_invalid_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTH_RUNTIME_MAX_RUNTIMES", "0")
    with pytest.raises(ValueError, match="TTH_RUNTIME_MAX_RUNTIMES"):
        asgi_mod._build_service()  # pyright: ignore[reportPrivateUsage]
