"""Credential changes over real HTTP connections, without transport mocks."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, cast
from uuid import uuid4

import pytest

from talktoharnesses.client import APIError, AsyncTalkToHarnessesClient, ConversationStreamItem


@pytest.fixture
def server() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"token": "new", "requests": [], "streams": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.handle_request()

        def do_POST(self) -> None:
            self.handle_request()

        def handle_request(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["requests"].append((self.command, self.path, dict(self.headers), body))
            is_stream = self.path.endswith("/events")
            status = 200
            if self.headers.get("Authorization") != "Bearer " + state["token"]:
                status = 401
                payload = b'{"code":"authentication_failed","message":"authentication failed"}'
            elif is_stream:
                state["streams"] += 1
                sequence = 7 + state["streams"]
                payload = (
                    f'id: {sequence}\nevent: sync\ndata: {{"sequence":{sequence}}}\n\n'.encode()
                )
            elif self.command == "POST":
                status = 422
                payload = b'{"code":"invalid_state","message":"test request received"}'
            else:
                payload = b'{"items":[],"next_cursor":null}'
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "text/event-stream" if is_stream and status == 200 else "application/json",
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/api/v1/", state
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


async def test_refresh_retries_only_401_and_preserves_mutation(
    server: tuple[str, dict[str, Any]],
) -> None:
    url, state = server
    tokens = iter(["old", "new"])

    async def provider() -> str:
        return next(tokens)

    async with AsyncTalkToHarnessesClient(url, token_provider=provider) as client:
        with pytest.raises(APIError) as error:
            await client.submit_turn(
                uuid4(), prompt="original prompt", idempotency_key="original-key"
            )
        assert error.value.status_code == 422
    first, second = state["requests"]
    assert first[0:2] == second[0:2]
    assert (
        first[3]
        == second[3]
        == json.dumps({"prompt": "original prompt"}, separators=(",", ":")).encode()
    )
    assert first[2]["Idempotency-Key"] == second[2]["Idempotency-Key"] == "original-key"
    assert first[2]["Authorization"] == "Bearer old"
    assert second[2]["Authorization"] == "Bearer new"


@pytest.mark.parametrize("values", [("old", "old"), ("old", "still-invalid")])
async def test_auth_retry_is_bounded(
    server: tuple[str, dict[str, Any]], values: tuple[str, str]
) -> None:
    url, state = server
    tokens = iter(values)

    async def provider() -> str:
        return next(tokens)

    async with AsyncTalkToHarnessesClient(url, token_provider=provider) as client:
        with pytest.raises(APIError) as error:
            await client.list_harnesses()
        assert error.value.status_code == 401
    assert len(state["requests"]) == (1 if values[0] == values[1] else 2)


async def test_stream_auth_retry_and_reconnect_preserve_cursor(
    server: tuple[str, dict[str, Any]],
) -> None:
    url, state = server
    tokens = iter(["old", "new", "rotated"])

    async def provider() -> str:
        return next(tokens)

    async with (
        AsyncTalkToHarnessesClient(url, token_provider=provider) as client,
        aclosing(
            cast(
                AsyncGenerator[ConversationStreamItem, None],
                client.stream_conversation_events(uuid4(), after_sequence=7),
            )
        ) as events,
    ):
        assert (await anext(events)).sequence == 8
        state["token"] = "rotated"
        assert (await anext(events)).sequence == 9
    headers = [request[2] for request in state["requests"]]
    assert [header["Authorization"] for header in headers] == [
        "Bearer old",
        "Bearer new",
        "Bearer rotated",
    ]
    assert [header["Last-Event-ID"] for header in headers] == ["7", "7", "8"]


async def test_fixed_tokens_never_reload_and_provider_cannot_rotate(
    server: tuple[str, dict[str, Any]],
) -> None:
    url, state = server
    async with AsyncTalkToHarnessesClient(url, token="old") as client:
        with pytest.raises(APIError):
            await client.list_harnesses()
    assert len(state["requests"]) == 1

    async def provider() -> str:
        return "new"

    with pytest.raises(ValueError, match="mutually exclusive"):
        AsyncTalkToHarnessesClient(url, token="fixed", token_provider=provider)
    async with AsyncTalkToHarnessesClient(url, token_provider=provider) as client:
        with pytest.raises(ValueError, match="credential store"):
            await client.rotate_token()
        with pytest.raises(ValueError, match="credential store"):
            await client.revoke_token()
    assert len(state["requests"]) == 1
