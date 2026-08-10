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
non-streaming requests.

## Token rotation and revocation

`rotate_token()` replaces the client's in-memory bearer token only after the
response validates as `TokenProjection`. Callers remain responsible for
securely persisting the returned token if it must survive process restart.

`revoke_token()` clears the client's token only after the server returns 204.

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
