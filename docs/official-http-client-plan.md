# Add the Official Async Python HTTP Client

## 1. Goal and fixed design

Ship a hand-written async Python client inside the existing `talktoharnesses` distribution.

The implementation must:

- Cover every operational route currently defined in `talktoharnesses.django.api.routes`.
- Reuse existing domain projections for responses rather than introducing duplicate client DTOs.
- Use `httpx.AsyncClient`.
- Keep `httpx` out of the core installation through a new `client` extra.
- Support automatic replay-safe SSE reconnection.
- Provide one typed exception for non-success HTTP responses.
- Remain async-only; do not add a sync facade, generated client, raw-request escape hatch, automatic pagination, turn-waiting helpers, or other convenience APIs.
- Leave `/openapi.json` and `/docs` unwrapped.
- Preserve all unrelated existing Cursor/ACP worktree changes.

The public import is:

```python
from talktoharnesses.client import (
    APIError,
    AsyncTalkToHarnessesClient,
    ConversationStreamItem,
)
```

Do not re-export these names from the package root; `talktoharnesses.__all__` remains `["__version__"]`.

## 2. Shared approval-rule input model

Add `ApprovalRuleInput` to `talktoharnesses.domain.models`, next to `ApprovalRule` and `ApprovalRuleProjection`.

Its fields are:

```python
class ApprovalRuleInput(BaseModel):
    decision: ApprovalRuleDecision
    scope: ApprovalRuleScope
    matcher: ApprovalMatcher
```

Implementation requirements:

- Use the frozen/forbid-extra domain configuration.
- Move the existing `scope` and `matcher` pre-validation logic from Django's `ApprovalRuleBody` into this shared model.
- Preserve discriminated-union parsing through `TypeAdapter(...).validate_json(...)`.
- Change Django's request model to:

```python
class ApprovalRuleBody(ApprovalRuleInput):
    model_config = _REQUEST
```

This retains the existing `ApprovalRuleBody` OpenAPI component name and non-strict wire coercion while keeping its fields and union validation single-sourced.

Export `ApprovalRuleInput` through `talktoharnesses.domain.__all__` and add it to the reviewed public-surface contract.

The HTTP client will accept `ApprovalRuleInput` for rule creation, rule replacement, and optional rule creation during interaction resolution.

## 3. Shared internal SSE decoder

Move the implementation currently in `talktoharnesses.providers.opencode.sse` into:

```text
src/talktoharnesses/_sse.py
```

The shared internal module continues to define `SseEvent` and `SseDecoder`.

Then:

- Update `OpenCodeAdapter` to import `SseDecoder` from `talktoharnesses._sse`.
- Update the existing decoder tests to import the shared internal module.
- Remove the old provider-specific `sse.py`; it is implementation-only and is not part of any approved `__all__`.
- Keep the existing parser behavior for UTF-8 chunking, LF/CRLF boundaries, comments, multiline `data`, `event`, and `id`.
- Change `max_partial_bytes` to `int | None`.
  - Preserve the existing 1 MiB default for OpenCode.
  - Skip the size check when it is `None`.
  - Instantiate the decoder with `max_partial_bytes=None` in the official client because server snapshot frames do not have the replay path's 5 MiB cap.

Do not add an SSE dependency or create a second decoder.

## 4. Public client types and lifecycle

Create:

```text
src/talktoharnesses/client.py
```

Its `__all__` is exactly:

```python
[
    "APIError",
    "AsyncTalkToHarnessesClient",
    "ConversationStreamItem",
]
```

### Optional dependency import

Import `httpx` lazily at module-import time with an actionable failure:

- If `httpx` itself is missing, raise `ModuleNotFoundError` explaining that `talktoharnesses[client]` must be installed.
- Do not mask an import failure originating from one of `httpx`'s own dependencies.
- Importing `talktoharnesses`, `talktoharnesses.domain`, or other core modules must not import `httpx`.

### APIError

Implement:

```python
class APIError(Exception):
    status_code: int
    code: str | None
    message: str
```

Behavior:

- For an unexpected HTTP status, try to validate the body as `ErrorProjection`.
- If validation succeeds, copy its stable `code` and safe `message`.
- If it fails, use `code=None` and `message="HTTP request failed"`.
- Do not retain or expose the raw response body, bearer token, or response object.
- Format `str(error)` with the status, optional code, and message.
- Wrong successful statuses, such as a 200 where the route contract requires 201, also raise `APIError`.
- Ordinary connection failures for non-streaming calls remain `httpx.TransportError`.
- Invalid JSON or schema-invalid successful bodies remain Pydantic `ValidationError`; do not wrap them.

### Stream item type

Define:

```python
ConversationStreamItem: TypeAlias = ConversationEvent | ConversationSnapshot | SyncProjection
```

### Client constructor and lifecycle

Implement this exact constructor:

```python
class AsyncTalkToHarnessesClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float | None = 30.0,
    ) -> None: ...
```

Required behavior:

- Require an absolute HTTP or HTTPS URL. Reject relative URLs with `ValueError`.
- Treat `base_url` as the mounted versioned API root, for example `https://host/api/v1/`.
- Normalize it to exactly one trailing slash.
- Create and own one `httpx.AsyncClient`.
- Set `User-Agent` to `talktoharnesses/<installed package version>`.
- Do not put the bearer token in the underlying client's persistent headers. Build the authorization header from the current token for each request so rotation and revocation affect subsequent calls.
- Expose a read-only `token: str | None` property.
- Implement `__aenter__`, `__aexit__`, and `aclose()`.
- `__aenter__` returns `self`.
- `__aexit__` calls `aclose()`.
- Do not add transport injection, proxy, TLS, retry, or arbitrary-header constructor options.

### Internal ordinary-request helper

Use one private helper for all non-streaming requests. It accepts:

- HTTP method.
- Relative path without a leading slash.
- Exact accepted status code or codes.
- Optional query parameters.
- Optional JSON body.
- Optional endpoint headers.

The helper must:

1. Add `Authorization: Bearer <token>` when a token exists.
2. Add endpoint headers such as `Idempotency-Key`.
3. Call the owned `httpx.AsyncClient`.
4. Compare the returned status against the exact accepted set.
5. Raise `APIError` through one shared error-parser when the status is not accepted.
6. Return the response without attempting endpoint-specific schema parsing.

Each public method then parses the response into its declared canonical type. Use `model_validate_json` for concrete Pydantic classes and module-level `TypeAdapter` instances for generic pages and tuples.

Never resolve endpoint URLs with a leading slash, because that would discard `/api/v1/` from the base URL.

## 5. Exact public endpoint methods

All IDs use `UUID`. Convert IDs to strings only while constructing paths or bodies. Omit query parameters whose value is `None`.

### System and authentication

| Client method | Request | Accepted status | Return |
| --- | --- | --- | --- |
| `health()` | `GET health` | 200 | `dict[str, str]` |
| `ready()` | `GET ready` | 200 or 503 | `ReadinessProjection` |
| `rotate_token()` | `POST auth/token/rotate` | 200 | `TokenProjection` |
| `revoke_token()` | `POST auth/token/revoke` | 204 | `None` |

Credential mutation rules:

- `rotate_token()` validates the complete `TokenProjection`, stores `projection.token` in the client, then returns the projection.
- If status or response validation fails, leave the old token unchanged.
- `revoke_token()` clears the token only after receiving 204.
- A missing token is allowed at construction time so `health()` and `ready()` can be called. Authenticated routes then naturally return `APIError(status_code=401, ...)`.

### Harnesses

| Client method | Request | Accepted status | Return |
| --- | --- | --- | --- |
| `list_harnesses(*, cursor=None, limit=50)` | `GET harnesses` | 200 | `Page[HarnessProjection]` |
| `create_harness(*, name, configuration)` | `POST harnesses` | 201 | `HarnessProjection` |
| `probe_harness(harness_id)` | `POST harnesses/{id}/probe` | 200 | `HarnessProbeProjection` |
| `get_harness_capabilities(harness_id)` | `GET harnesses/{id}/capabilities` | 200 | `HarnessProbeProjection` |
| `get_harness_models(harness_id)` | `GET harnesses/{id}/models` | 200 | `tuple[HarnessModelInfo, ...]` |
| `get_harness_modes(harness_id)` | `GET harnesses/{id}/modes` | 200 | `tuple[HarnessModeInfo, ...]` |

`create_harness` accepts `HarnessConfiguration` and sends:

```python
{
    "name": name,
    "configuration": configuration.model_dump(
        mode="json",
        exclude_none=True,
    ),
}
```

Page queries always send `limit` and send `cursor` only when non-`None`.

### Conversations and transcripts

| Client method | Request | Accepted status | Return |
| --- | --- | --- | --- |
| `list_conversations(*, cursor=None, limit=50, include_archived=True)` | `GET conversations` | 200 | `Page[ConversationShell]` |
| `create_conversation(harness_id, *, title=None)` | `POST conversations` | 201 | `ConversationSnapshot` |
| `import_transcript(harness_id, document)` | `POST conversations/import` | 201 | `ConversationSnapshot` |
| `search_conversations(query, *, cursor=None, limit=50)` | `GET conversations/search` | 200 | `Page[ConversationSearchHit]` |
| `get_conversation(conversation_id)` | `GET conversations/{id}` | 200 | `ConversationSnapshot` |
| `archive_conversation(conversation_id)` | `POST conversations/{id}/archive` | 200 | `ConversationSnapshot` |
| `unarchive_conversation(conversation_id)` | `POST conversations/{id}/unarchive` | 200 | `ConversationSnapshot` |
| `pin_conversation(conversation_id)` | `POST conversations/{id}/pin` | 200 | `ConversationSnapshot` |
| `unpin_conversation(conversation_id)` | `POST conversations/{id}/unpin` | 200 | `ConversationSnapshot` |
| `snooze_conversation(conversation_id, *, until)` | `POST conversations/{id}/snooze` | 200 | `ConversationSnapshot` |
| `unsnooze_conversation(conversation_id)` | `POST conversations/{id}/unsnooze` | 200 | `ConversationSnapshot` |
| `delete_conversation(conversation_id)` | `DELETE conversations/{id}` | 204 | `None` |
| `set_retention_exemption(conversation_id, *, exempt)` | `PUT conversations/{id}/retention-exemption` | 200 | `ConversationSnapshot` |
| `export_transcript(conversation_id)` | `GET conversations/{id}/transcript` | 200 | `TranscriptDocument` |

Serialization rules:

- `create_conversation` sends `{"harness_id": str(harness_id)}` and includes `title` only when non-`None`.
- `import_transcript` accepts only `TranscriptDocument` and sends its `model_dump(mode="json")` under `document`.
- `search_conversations` sends query parameter `q`, plus normal page parameters.
- `list_conversations` always sends `include_archived` and `limit`.
- `snooze_conversation` sends `until.isoformat()`; server-side validation remains authoritative for timezone requirements.
- `set_retention_exemption` sends `{"exempt": exempt}`.

### Retention

| Client method | Request | Accepted status | Return |
| --- | --- | --- | --- |
| `get_retention_policy()` | `GET retention` | 200 | `RetentionPolicyProjection` |
| `replace_retention_policy(months)` | `PUT retention` | 200 | `RetentionPolicyProjection` |
| `preview_retention()` | `GET retention/preview` | 200 | `RetentionPreviewProjection` |

`replace_retention_policy` sends `{"months": months}` and does not duplicate the server's numeric-range validation.

### History pages

Each method accepts `conversation_id`, `cursor: str | None = None`, and `limit: int = 50`.

| Client method | Path suffix | Return |
| --- | --- | --- |
| `page_turns(...)` | `turns` | `Page[TurnProjection]` |
| `page_messages(...)` | `messages` | `Page[MessageProjection]` |
| `page_tools(...)` | `tools` | `Page[ToolProjection]` |
| `page_plans(...)` | `plans` | `Page[PlanProjection]` |
| `page_activity(...)` | `activity` | `Page[ActivityProjection]` |

All use `GET conversations/{conversation_id}/{suffix}`, accept only status 200, always send `limit`, and omit a `None` cursor.

Do not consolidate these into a generic public history method.

### Turn and conversation control

| Client method | Request | Accepted status | Return |
| --- | --- | --- | --- |
| `submit_turn(conversation_id, *, prompt, idempotency_key, model=None)` | `POST conversations/{id}/turns` | 202 | `SubmitTurnResult` |
| `edit_queued_prompt(conversation_id, *, prompt)` | `PATCH conversations/{id}/queued-prompt` | 200 | `ConversationSnapshot` |
| `cancel_queued_prompt(conversation_id)` | `DELETE conversations/{id}/queued-prompt` | 200 or 204 | `CommandProjection | None` |
| `steer(conversation_id, *, prompt, idempotency_key)` | `POST conversations/{id}/steer` | 202 | `CommandProjection` |
| `switch_harness(conversation_id, *, harness_id, idempotency_key)` | `POST conversations/{id}/switch` | 202 | `CommandProjection` |
| `interrupt(conversation_id)` | `POST conversations/{id}/interrupt` | 202 | `CommandProjection` |

Rules:

- Put `Idempotency-Key` in the header for `submit_turn`, `steer`, and `switch_harness`.
- Do not invent or normalize idempotency keys in the client.
- `submit_turn` sends `prompt` and includes `model` only when non-`None`.
- `steer` sends `{"prompt": prompt}`.
- `switch_harness` sends `{"harness_id": str(harness_id)}`.
- `interrupt` sends no body and no idempotency header because the route does not accept one.
- `cancel_queued_prompt` returns `None` for 204 and validates `CommandProjection` for 200.

### Interactions

| Client method | Request | Accepted status | Return |
| --- | --- | --- | --- |
| `list_interactions(conversation_id, *, cursor=None, limit=50)` | `GET conversations/{id}/interactions` | 200 | `Page[InteractionProjection]` |
| `update_interaction_draft(conversation_id, interaction_id, *, draft)` | `PATCH conversations/{id}/interactions/{interaction_id}/draft` | 200 | `InteractionProjection` |
| `resolve_interaction(conversation_id, interaction_id, *, decision=None, answers=None, create_rule=None)` | `POST conversations/{id}/interactions/{interaction_id}/resolve` | 202 | `CommandProjection` |

`resolve_interaction` types:

```python
decision: ApprovalDecision | None
answers: dict[str, Any] | None
create_rule: ApprovalRuleInput | None
```

Build its body by including only non-`None` values:

- `decision.value`
- `answers`
- `create_rule.model_dump(mode="json")`

Do not add client-side validation that attempts to decide which combinations are valid; let the canonical server validation return a typed API error.

### Approval rules and interaction audits

| Client method | Request | Accepted status | Return |
| --- | --- | --- | --- |
| `list_approval_rules(*, cursor=None, limit=50)` | `GET approval-rules` | 200 | `Page[ApprovalRuleProjection]` |
| `create_approval_rule(rule)` | `POST approval-rules` | 201 | `ApprovalRuleProjection` |
| `get_approval_rule(rule_id)` | `GET approval-rules/{id}` | 200 | `ApprovalRuleProjection` |
| `replace_approval_rule(rule_id, rule)` | `PUT approval-rules/{id}` | 200 | `ApprovalRuleProjection` |
| `delete_approval_rule(rule_id)` | `DELETE approval-rules/{id}` | 204 | `None` |
| `list_interaction_audits(*, cursor=None, limit=50)` | `GET interaction-audits` | 200 | `Page[InteractionAuditProjection]` |
| `get_interaction_audit(audit_id)` | `GET interaction-audits/{id}` | 200 | `InteractionAuditProjection` |

Both rule-writing methods accept `ApprovalRuleInput` and send `rule.model_dump(mode="json")`.

Do not accept `ApprovalRule` or `ApprovalRuleProjection` for writes because their server-owned ID, principal, and timestamps are not part of the request.

## 6. SSE event streaming

Add:

```python
def stream_conversation_events(
    self,
    conversation_id: UUID,
    *,
    after_sequence: int = 0,
) -> AsyncIterator[ConversationStreamItem]: ...
```

Implement it as an async generator.

### Opening each attempt

For each connection attempt:

- Request `GET conversations/{conversation_id}/events`.
- Add the current bearer token.
- Initial attempt:
  - Send `Last-Event-ID` when `after_sequence != 0`.
- Reconnection attempt:
  - Always send `Last-Event-ID`, including `"0"`, so the server can record the reconnect.
- Use the constructor timeout for connect, write, and pool.
- Disable only the stream read timeout.
- Require status 200.
- On another status, fully read the response body, raise `APIError`, and do not reconnect.
- Require a `Content-Type` beginning with `text/event-stream`; otherwise raise `ValueError` without retrying.

Do not reuse a decoder across connection attempts. A truncated partial frame from a failed connection must be discarded before replay.

### Parsing frames

Ignore comment-only keepalive frames.

For every data frame:

1. Require `event`, `id`, and `data`.
2. Parse `id` as an integer.
3. Select the model:
   - `event == "snapshot"` -> `ConversationSnapshot.model_validate_json(data)`
   - `event == "sync"` -> `SyncProjection.model_validate_json(data)`
   - Any other event name -> `conversation_event_adapter.validate_json(data)`
4. For canonical conversation events, require the SSE `event` name to equal `ConversationEvent.type`.
5. Require the numeric SSE ID to equal the parsed object's `sequence`.
6. Raise `ValueError` for missing/mismatched metadata.
7. Allow Pydantic validation errors to propagate unchanged.
8. Update the reconnect cursor before yielding the parsed item.

Do not expose raw `SseEvent` objects publicly.

### Reconnection behavior

Reconnect only after:

- Clean response EOF without a deletion event.
- `httpx.TransportError` while opening or reading the stream.

Do not reconnect after:

- `APIError`
- Invalid content type
- Invalid SSE metadata
- Invalid JSON or Pydantic schema
- Client closure/runtime errors
- Cancellation

Backoff schedule:

```text
1, 2, 4, 8, 16, 30, 30, ... seconds
```

Rules:

- Reset the next delay to one second after every successfully validated stream item.
- Sleep before opening the next attempt.
- Use `asyncio.sleep`.
- Do not add jitter or a configurable retry policy.
- Let `asyncio.CancelledError` propagate.
- Ensure the active response context is closed when the generator is cancelled or closed.

A `ConversationEvent` with `ConversationMetadataChangedPayload.deleted_at` set is terminal:

- Yield the deletion event once.
- Return from the generator without reconnecting after the consumer resumes it.

Snapshot and sync frames may legitimately share the same sequence. Do not reject them as duplicates.

## 7. Packaging and dependency changes

Update `pyproject.toml`:

```toml
[project.optional-dependencies]
client = [
    "httpx>=0.28,<0.29",
]
opencode = [
    "talktoharnesses[client]",
]
```

Update `all` to include `client` explicitly alongside the existing extras.

This makes the `client` extra the only declaration of the supported `httpx` version range. Do not repeat that version constraint in the development dependency group.

Update `uv.lock`.

Update packaging tests:

- Add `"client"` to `REQUIRED_EXTRAS`.
- Core isolated install:
  - Continue asserting `find_spec("httpx") is None`.
  - Verify importing `talktoharnesses.client` without `httpx` raises the actionable optional-extra error.
- New `[client]` isolated install:
  - Import `AsyncTalkToHarnessesClient`, `APIError`, and `ConversationStreamItem`.
  - Assert `httpx` is installed.
  - Assert Django, Ninja, JWT, Psycopg, Codex, and Claude SDKs are absent.
  - Construct and close a client without making a network request.
- Existing `[opencode]` and `[all]` installations must still install `httpx`.

Update the static CI dependency sync from:

```text
uv sync --locked --extra django
```

to:

```text
uv sync --locked --extra django --extra client
```

The full coverage/build jobs already install `all` and therefore need no separate client flag.

Update the README development setup command to install both `django` and `client` before Ruff/Pyright/tests.

## 8. Documentation

Add `docs/http-client.md` and link it from the README operational-document list.

The guide must include:

- `pip install "talktoharnesses[client]"`
- `uv add "talktoharnesses[client]"`
- A complete `async with AsyncTalkToHarnessesClient(...)` example.
- The requirement that `base_url` includes the mounted `/api/v1/` prefix.
- Harness creation and conversation creation using `HarnessConfiguration`.
- Turn submission with an explicit idempotency key.
- Manual page traversal with `next_cursor`.
- `APIError` handling using `status_code`, `code`, and `message`.
- Token rotation, noting that the client updates its in-memory token and callers remain responsible for securely persisting the returned token.
- Token revocation, noting that it clears the client token.
- SSE consumption with `isinstance` checks for `ConversationEvent`, `ConversationSnapshot`, and `SyncProjection`.
- Automatic SSE reconnect/backoff and `after_sequence`.
- A statement that ordinary HTTP calls are not retried.
- A statement that this client does not sandbox harness execution and does not add authorization beyond the server's bearer token.

Keep endpoint-level reference documentation in the existing generated OpenAPI docs; do not duplicate every method's prose in the guide.

## 9. Tests

### Shared approval input tests

Update existing Django schema tests to prove:

- Raw JSON enum strings still validate through `ApprovalRuleBody`.
- Extra fields remain rejected.
- Invalid discriminators remain rejected.
- `ApprovalRuleInput` constructed from canonical enums/scopes/matchers serializes to the same wire shape used by Django.

### Ordinary client tests

Create `tests/unit/test_client.py`.

Use `httpx.MockTransport` and a recording handler. Since transport injection is intentionally not public, patch the module's `httpx.AsyncClient` constructor in tests with a factory that supplies the mock transport while preserving the real class.

Cover every public non-streaming method. For each method assert:

- HTTP method.
- Full URL retains `/api/v1/`.
- UUID path rendering.
- Query parameter inclusion/omission.
- Exact request JSON.
- Bearer and idempotency headers.
- Exact accepted status.
- Returned Python type and representative field values.

Build valid response payloads from canonical domain models and `model_dump(mode="json")`; do not hand-maintain duplicated JSON schemas.

Add focused tests for:

- Base URLs with and without a trailing slash.
- Relative base URL rejection.
- Token omitted and token supplied.
- 401 parsed into `APIError`.
- A known domain conflict parsed into `APIError`.
- Malformed/non-JSON error response producing `code=None` and the generic message.
- Unexpected 2xx status producing `APIError`.
- Malformed successful response producing Pydantic `ValidationError`.
- `ready()` returning a false `ReadinessProjection` for 503 rather than raising.
- `cancel_queued_prompt()` returning a command for 200 and `None` for 204.
- Rotation replacing the token only after successful validation.
- Failed rotation retaining the old token.
- Revocation clearing the token only after 204.
- Context-manager and explicit-close behavior.

### SSE client tests

Use mock streaming responses backed by a small test `httpx.AsyncByteStream`.

Cover:

- Event, snapshot, and sync parsing.
- Fragmented UTF-8 and fragmented frame boundaries.
- CRLF frames.
- Multiline data.
- Keepalive comments being ignored.
- Initial `after_sequence` header.
- Reconnect `Last-Event-ID` using the most recently yielded sequence.
- A clean EOF reconnect.
- A read-time `httpx.TransportError` reconnect.
- Backoff progression through 1/2/4/8/16/30 and the 30-second cap.
- Backoff reset after a valid item.
- Cancellation during stream reading and during backoff.
- Response context closure after cancellation.
- Terminal deletion event yielding once and preventing reconnect.
- Non-200 response raising `APIError` without retry.
- Invalid content type without retry.
- Missing/non-integer/mismatched SSE IDs.
- Mismatched SSE event name and `ConversationEvent.type`.
- Invalid JSON and unknown canonical event payloads without retry.
- Snapshot and sync sharing a sequence without failure.

Patch `asyncio.sleep` in retry tests; do not make tests wait in real time.

### Real Django-ASGI smoke test

Extend the existing Phase 5 API gate rather than creating another duplicate service fixture.

Using its installed service and users:

1. Create an `httpx.ASGITransport` around Django's ASGI application.
2. Patch only the client's internal `httpx.AsyncClient` construction to use that transport.
3. Construct the official client with `http://testserver/api/v1/` and a real issued bearer token.
4. Call `health()`.
5. Create a harness.
6. Create a conversation.
7. Submit a turn with an idempotency key.
8. Assert canonical return types and IDs.
9. Use the second user's token to request the first conversation and assert `APIError` with status 404 and code `not_found`.

Keep SSE reconnection testing in the mocked unit suite because the real endpoint intentionally stays open.

### Public surface and quality gates

Update the reviewed public-surface test with:

- `talktoharnesses.client` exports exactly the three client names.
- `talktoharnesses.domain` includes `ApprovalRuleInput`.

Run:

```text
uv lock
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --ignore=tests/live --cov=talktoharnesses --cov-fail-under=91
uv build --no-sources
uv run pytest tests/test_packaging.py -q
```

## 10. Explicit non-goals and assumptions

- No sync client.
- No TypeScript or other language package.
- No generated OpenAPI client or generated models.
- No wrapper for `/openapi.json` or `/docs`.
- No automatic pagination.
- No automatic idempotency-key generation.
- No ordinary-request retries.
- No wait/poll-until-turn-complete helper.
- No public raw `httpx.AsyncClient` access.
- No public transport injection or arbitrary request method.
- No client-side duplication of server business validation.
- No package version bump; keep `2026.8.1` unless release work is separately requested.
- Do not modify compatibility matrices, provider versions, migrations, or unrelated dirty Cursor/ACP files.
