# Official async HTTP client

The package ships a hand-written async client for the Django HTTP/SSE surface.
Install the optional extra before importing it:

```bash
pip install "talktoharnesses[client]"
# or
uv add "talktoharnesses[client]"
```

```python
from talktoharnesses.client import (
    APIError,
    AsyncTalkToHarnessesClient,
)
```

`base_url` must be the mounted versioned API root and must include `/api/v1/`.

## Quick start

```python
import asyncio
from uuid import UUID

from talktoharnesses.client import APIError, AsyncTalkToHarnessesClient
from talktoharnesses.domain import (
    ConversationEvent,
    ConversationSnapshot,
    HarnessConfiguration,
    HarnessKind,
    SyncProjection,
)


async def main() -> None:
    async with AsyncTalkToHarnessesClient(
        "https://example.invalid/api/v1/",
        token="replace-with-bearer-token",
    ) as client:
        print(await client.health())

        harness = await client.create_harness(
            name="local",
            configuration=HarnessConfiguration(
                kind=HarnessKind.GROK,
                working_directory="/workspace",
            ),
        )
        same_harness = await client.get_harness(harness.id)
        print(same_harness.name)
        snapshot = await client.create_conversation(
            harness.id,
            title="First conversation",
        )
        conversation_id = snapshot.detail.conversation.id

        turn = await client.submit_turn(
            conversation_id,
            prompt="Summarize the repository layout",
            idempotency_key="client-turn-1",
        )
        print(turn.command.id, turn.turn.status)

        page = await client.list_conversations(limit=20)
        while True:
            for shell in page.items:
                print(shell.id, shell.title)
            if page.next_cursor is None:
                break
            page = await client.list_conversations(
                cursor=page.next_cursor,
                limit=20,
            )

        async for item in client.stream_conversation_events(
            conversation_id,
            after_sequence=0,
        ):
            if isinstance(item, ConversationSnapshot):
                print("snapshot", item.sequence)
            elif isinstance(item, SyncProjection):
                print("sync", item.sequence)
            elif isinstance(item, ConversationEvent):
                print("event", item.type, item.sequence)


asyncio.run(main())
```

`list_harnesses()` and `get_harness()` support selecting an existing
owner-scoped harness. `delete_harness()` removes an idle harness and returns
normally on HTTP 204; the server returns `harness_in_use` (409) while a bound
conversation is active. Retained conversations keep their copied binding
configuration after the harness record is deleted.

## Errors

Non-success statuses raise `APIError` with `status_code`, optional stable
`code`, and a safe `message`. Ordinary connection failures remain
`httpx.TransportError`. Invalid successful JSON remains a Pydantic
`ValidationError`.

```python
try:
    await client.get_conversation(conversation_id)
except APIError as exc:
    print(exc.status_code, exc.code, exc.message)
```

Ordinary HTTP calls are not retried. Callers own backoff and retries for
non-streaming requests. `APIError.retry_after` carries the response's
`Retry-After` in seconds (delta-seconds or HTTP-date), or `None` when the
header is absent or invalid. The API sends it with
`503 worker_unavailable`, returned for new turns, steers, interrupts, and
harness switches while the command worker cannot take commands (for example
while it reacquires its lease after the host slept), with the worker's lease
renewal interval as the delay; retry with the same idempotency key after it.

The idempotent commands, `submit_turn`, `steer`, and `switch_harness`, are the
one exception, opt-in through the constructor's `command_retry_seconds`
(default `0`, no retry): they retry `503 worker_unavailable` with the same
idempotency key after each `Retry-After` (1 s when absent, never under 0.1 s)
until that budget is spent, then raise the last refusal. Every other error is
raised at once. `interrupt` carries no idempotency key and is never retried.

```python
client = AsyncTalkToHarnessesClient(base_url, token=token, command_retry_seconds=30)
```

## Timeouts

The client constructor accepts `timeout` (default `30.0` seconds). Pass
`timeout=None` to disable timeouts client-wide.

Every public method also accepts an optional keyword-only `timeout=`. Omit it
to inherit the client value; pass a float to override that call; pass `None`
to disable timeouts for that call only:

```python
await client.probe_harness(harness_id, timeout=120.0)
```

For `stream_conversation_events`, `timeout=` bounds connect, write, and pool
only. The stream read timeout stays disabled so long-lived SSE connections are
not cut off.

## Token rotation and revocation

`rotate_token()` replaces the client's in-memory bearer token only after the
response validates as `TokenProjection`. Callers remain responsible for
securely persisting the returned token if it must survive process restart.

`revoke_token()` clears the client's token only after the server returns 204.

For a credential shared by several processes, pass an async `token_provider`
instead of `token`. The provider returns the current bearer token from your
credential store. The client calls it before every HTTP request and every SSE
connection, including reconnections. A 401 triggers one more provider lookup;
the client retries once only if the token changed. The retry preserves the
request body, idempotency key, and stream cursor. Other failures do not trigger
an authentication retry.

```python
async with AsyncTalkToHarnessesClient(base_url, token_provider=load_current_token) as client:
    harnesses = await client.list_harnesses()
```

`token` and `token_provider` are mutually exclusive. Provider-mode clients reject
`rotate_token()` and `revoke_token()` because the credential store owns those
operations. Use a separate fixed-token client in the store's rotation service.
The `token` property reports only the fixed token, or `None` in provider mode.

Credential stores can also pass an async `on_token_rejected(token)` callback.
It requires `token_provider` and receives the token rejected by the final 401,
after the changed-token retry is exhausted or the provider returns the same token.
The callback runs before callers receive an `APIError`, for both HTTP and SSE.
It can record that the credential needs replacement and raise an actionable
application error. Match the rejected token to the current stored credential
before changing it, since another request may already have installed a replacement.

HTTP and SSE share one HTTPX authentication flow. Each connection starts a new
flow, so a dropped connection cannot consume a later connection's authentication
retry. Request bodies, idempotency headers, and SSE cursors remain on the request.

## SSE streaming

`stream_conversation_events` reconnects after clean EOF and transport errors
using a fixed backoff of 1, 2, 4, 8, 16, then 30 seconds (capped). The next
delay resets to one second after every successfully validated stream item.
Pass `after_sequence` to resume from a known high-water mark.

Check item types with `isinstance` against `ConversationEvent`,
`ConversationSnapshot`, and `SyncProjection`. A metadata deletion event is
terminal: the generator yields it once and stops without reconnecting.

## Security notes

This client does not sandbox harness execution. Authorization is limited to the
server's bearer token; the package does not add additional access controls.

Endpoint-level request and response shapes remain documented by the generated
OpenAPI surface at `/docs` and `/openapi.json` on a running server.
