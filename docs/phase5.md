# Phase 5 — Python Facade, Django-Ninja API, JWT, and SSE

## Summary

- Merge the completed Phase 4 branch first, then implement Phase 5 as version
  `2026.8.0.dev5`.
- Add `TalkToHarnessesService` as the single asynchronous Python facade over persistence,
  adapters, durable commands, projections, and event replay.
- Add a versioned Django-Ninja API at `/api/v1`, authenticated with revocable HS256 bearer
  tokens except for health, readiness, OpenAPI, and documentation.
- Stream committed conversation events with replay-safe SSE. A request or SSE disconnect never
  owns or cancels a harness turn.
- Keep the core package Django-free. Django, Django-Ninja, PyJWT, Uvicorn, ORM models, and the ASGI
  wrapper remain inside the `django` feature boundary.

## Phase Boundary and Preconditions

Phase 5 starts only after the Phase 4 gate passes. Before adding the facade, reconcile the merged
implementation with the Phase 2 and Phase 4 contracts:

- Durable turn, message, reasoning, plan, tool, usage, interaction, activity, harness/probe, token,
  and sanitized search-document rows must exist. Aggregate JSON and the event log cannot be the
  only history source because Phase 5 requires paginated projections and bounded detail snapshots.
- Command acceptance must atomically persist the command, projection changes, and canonical events.
  Interaction resolution must create the executable `answer_interaction` command consumed by the
  Phase 4 worker.
- The Phase 4 command processor, publisher-after-commit path, lazy resume, and Grok vertical-slice
  tests must be complete. Phase 5 must not compensate for an incomplete worker in HTTP handlers.
- The common SQLite/PostgreSQL persistence contract must already cover the projection rows used by
  the facade. Missing prerequisite behavior is fixed on the owning phase before Phase 5 begins,
  instead of adding a second Phase 5 representation.

The high-level milestone mentions a switch endpoint, but Phase 8 owns the candidate-runtime
transaction, transcript handoff, rollback, and switch events. Phase 5 does not publish a dead route,
accept a command that cannot execute, or return a temporary stub. The switch endpoint is added with
the Phase 8 implementation.

Phase 5 does implement functional archive, pin, snooze, soft-delete, lightweight list projections,
and portable search behavior because its conversation API depends on them. Phase 8 may replace the
query/index backend and centralize richer projection rules, but must preserve the Phase 5 facade and
wire models.

Readiness in this phase means that the database is reachable and this process has successfully
started its command worker and event broker. Phase 9 extends that same endpoint with recent harness
probe and recovery checks; Phase 5 does not pre-implement those checks.

## Verified Framework Baseline

- Django 5.2 supports long-lived SSE under ASGI with an asynchronous iterator passed to
  `StreamingHttpResponse`. Client disconnect cancels the streaming coroutine, so subscription
  cleanup belongs in the iterator's `finally` path
  ([Django streaming responses](https://docs.djangoproject.com/en/5.2/ref/request-response/#django.http.StreamingHttpResponse)).
- Django documents wrapping its ASGI application in the host project's `asgi.py`. The package uses
  that composition point for lifespan handling; it does not start workers from `AppConfig.ready()`
  ([Django ASGI deployment](https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/)).
- Django-Ninja supports asynchronous operations and custom bearer authentication. Core Pydantic
  models can be used directly as response schemas, avoiding parallel HTTP-only output models
  ([async support](https://django-ninja.dev/guides/async-support/),
  [authentication](https://django-ninja.dev/guides/authentication/)).
- PyJWT validates expiration, issuer, and audience while decode is restricted to an explicit
  algorithm allowlist. The implementation always supplies `algorithms=["HS256"]`; token headers
  never choose the accepted algorithm
  ([PyJWT usage](https://pyjwt.readthedocs.io/en/latest/usage.html)).
- Psycopg 3 supports PostgreSQL `LISTEN`/`NOTIFY`. A dedicated autocommit listener is used only as a
  wakeup channel; durable event rows remain authoritative
  ([Psycopg notifications](https://www.psycopg.org/psycopg3/docs/advanced/async.html#asynchronous-notifications)).

## Public Contracts

### Asynchronous facade

Export `TalkToHarnessesService` from `talktoharnesses.application.service`. Its constructor receives
exactly the existing application/runtime authorities:

- `Persistence`
- `AdapterRegistry`
- a committed-event broker implementing `CommittedEventPublisher`
- a UTC clock
- `RuntimeManager`

The service constructs and owns the Phase 4 `CommandProcessor`. `start(worker_id)` and `stop()` are
explicit, asynchronous, and idempotent. `stop()` first stops new command claims, then delegates to
the established command-processor/runtime shutdown path and closes event-broker resources. No
synchronous facade or implicit event loop is added.

Public facade methods accept an explicit `owner_id: str`. They return the same frozen Pydantic
models used by HTTP JSON and SSE snapshot data. The initial surface is grouped as follows:

- Harnesses: create/list configured harnesses, probe one harness, and read its last successfully
  probed capabilities, models, and modes.
- Conversations: create, list/search, get detail, archive/unarchive, pin/unpin, snooze/unsnooze,
  and soft-delete.
- History: page turns, messages, tools, plans, and background activity.
- Turn control: submit, edit/cancel the queued prompt, steer, and interrupt.
- Interactions: list pending interactions, update a draft, and resolve once.
- Event sync: build a sequence-stamped detail snapshot and replay committed events.

Every method scopes by owner before resolving a conversation or harness UUID. Worker-only methods
remain owner-independent and private to the command processor.

### Shared Pydantic projections

Reuse and extend the existing projection models in `talktoharnesses.domain.models`; do not create
Ninja `ModelSchema` copies. Add only the missing reusable wire models:

- `Page[T]` with `items` and `next_cursor`.
- `HarnessProjection` and `HarnessProbeProjection`.
- Complete `TurnProjection`, `MessageProjection`, `ToolProjection`, `PlanProjection`,
  `ActivityProjection`, and `InteractionProjection` fields required by their routes.
- `ConversationSnapshot` containing `sequence` and `ConversationDetail`.
- `SyncProjection` containing the last synchronized sequence.
- `SubmitTurnResult` containing the durable `CommandProjection` and current target
  `TurnProjection`.
- `TokenProjection` containing the bearer token and its expiry. It never contains the raw `jti`.
- One generic public `ErrorProjection` containing a stable code and generic message.

Request-only models live beside the API routers and may differ from output models. Canonical
events continue to use `ConversationEvent` directly. SSE serializes these models with
`model_dump_json()`; Ninja returns the same model instances and validates them against the same
classes.

### Cursor contract

Use unsigned URL-safe base64-encoded JSON containing only a sort value and UUID tie-breaker. The
cursor is opaque, versionless, and not a security boundary; every query reapplies owner and filter
scope. Invalid cursors fail with `invalid_cursor` rather than falling back to the first page.

Ordering is deterministic:

- Conversation lists/search: `(updated_at DESC, id DESC)`.
- Turns and their child resources: their persisted canonical order, followed by UUID as a stable
  tie-breaker.
- Pending interactions: `(created_at ASC, id ASC)`.

One cursor encoder/decoder is shared by the facade queries. Do not add offset pagination or expose
cursor fields as a public compatibility promise. Collection endpoints default to 50 items and
accept a maximum of 200.

## Work Package 1 — Owner-Scoped Projection Persistence

Extend `Persistence` only with coarse operations required by the facade:

- Owner-scoped harness create/list/probe reads and writes.
- Owner-scoped conversation list, search, detail snapshot, and history-page reads.
- An atomic owner-scoped state/event/command commit for facade mutations.
- A sequence-stamped snapshot read whose projection and high-water sequence are observed in one
  database transaction.
- A committed high-water read used to decide whether bounded replay was truncated.

Do not expose querysets or generic ORM CRUD. Django transaction bodies remain synchronous functions
called through the existing thread-sensitive async bridge.

Facade mutation commits must:

1. Select the owner-scoped aggregate and reject a cross-owner UUID exactly like a missing UUID.
2. Check the expected aggregate version.
3. Insert or update command/projection rows and sanitized search documents.
4. Insert canonical events and advance the conversation-local sequence.
5. Commit the materialized aggregate in the same transaction.
6. Return only the committed events, which are then passed to the broker.

Add the minimal pure transitions and canonical payload needed for archive, pin, snooze, and
soft-delete. Use one `conversation_metadata_changed` payload containing the complete current values
of `archived_at`, `pinned_at`, `snoozed_until`, and `deleted_at`; this avoids four competing update
rules and lets replay clients replace those fields directly. Deleted conversations disappear from
ordinary list, detail, search, mutation, and SSE authorization queries.

Archive and soft-delete require no active turn, queued prompt, or running background activity;
otherwise return `conversation_busy`. Pin/unpin and snooze/unsnooze do not affect execution state.
An existing SSE subscriber receives the committed soft-delete event and then closes; a new or
reconnecting subscriber receives the same owner-scoped 404 as any deleted conversation.

Build each detail snapshot from the newest 20 turns that have a persisted user message. Include all
retained child projections for those selected turns plus current pending interactions and the active
command. A sparse or tool-only history must not cause older user-anchored turns to be skipped.

## Work Package 2 — Portable Search and Query Projections

Keep one sanitized search-document builder in the application layer. It accepts canonical retained
models and emits normalized text for:

- user and assistant messages;
- canonical tool name, normalized arguments, paths, and the existing 2 KiB output tail; and
- the effective conversation title.

Exclude reasoning, raw/native events, stderr, secrets removed by the central redactor, and full raw
tool output. Update the affected search document in the same transaction as its projection.

The Phase 5 backend applies one case-insensitive substring match for the normalized query over
normalized search-document rows and returns distinct owner-scoped conversations in normal
conversation-list order. Correctness takes precedence over ranking. Phase 8 replaces this query
implementation with PostgreSQL vectors and SQLite FTS5 while continuing to call the same document
builder and return the same `Page[ConversationShell]`.

List queries derive `ConversationShell` in the repository rather than loading full aggregate JSON
for every row. Index owner/deletion/update fields and the foreign keys used by detail/history
queries. Do not add database-specific search indexes in this phase.

## Work Package 3 — JWT Issuance, Authentication, Rotation, and Revocation

Add PyJWT to the `django` extra. Add only these host settings:

- `TALKTOHARNESSES_JWT_SIGNING_KEY`: required, at least 32 bytes, and rejected if equal to Django's
  `SECRET_KEY`.
- `TALKTOHARNESSES_TOKEN_TTL`: optional `timedelta`, default 30 days, required to be positive.

Use fixed claims `iss="talktoharnesses"` and `aud="talktoharnesses-api"`. Each token contains the
string Django user primary key as `sub`, a cryptographically random `jti`, UTC `iat`, and UTC `exp`.
Decode requires `sub`, `jti`, `iat`, and `exp`, validates issuer/audience/expiry, and hard-codes
HS256.

Store `sha256(jti)` only, in one row with a unique/one-to-one reference to
`settings.AUTH_USER_MODEL`, plus issue and expiry timestamps. `issue_token(user)` is a trusted
asynchronous in-process function: inside one transaction it locks/replaces the user's active token
row and returns `TokenProjection`. Issuing a second token immediately invalidates the first.
These focused Django auth transactions stay in `talktoharnesses.django.auth`; they do not add a
Django user dependency to the core `Persistence` protocol or application facade.

Bearer authentication performs these checks on every request:

1. Decode and validate all required JWT claims and the HS256 signature.
2. Load the user from the swappable model by `sub` and require `is_active`.
3. Compare the stored digest and presented `jti` digest with constant-time comparison.
4. Place the authenticated user on `request.auth`; derive `owner_id` only from that object.

All failures—including malformed headers, bad signatures/claims, missing users, inactive users,
revocation, and superseded tokens—return the same 401 body and `WWW-Authenticate: Bearer`. Logs do
not include bearer tokens, claims, or raw `jti` values.

Rotation locks and verifies the currently presented token row before replacing it. In a concurrent
rotation race, exactly one request succeeds and every loser receives the generic authentication
failure because its token is no longer active. Revocation conditionally deletes the row matching
the current digest and returns 204; the revoked token fails its next request.

Validate the signing key and TTL when constructing the ASGI/API authentication surface and fail
startup on invalid configuration. Django management commands that only load the persistence app do
not require the API secret. No insecure development default is provided.

## Work Package 4 — Committed-Event Broker and SSE

Add a narrow `CommittedEventBroker` protocol extending `CommittedEventPublisher` with explicit
`start()`, `stop()`, and conversation wakeup subscription. The Phase 4 command processor continues
to depend only on `publish()`.

The Django implementation treats wakeups as hints:

- PostgreSQL publishes `pg_notify` only after the event transaction has committed. One dedicated
  autocommit listener connection receives notifications for this process.
- SQLite uses an in-process condition plus short polling, consistent with its documented
  single-supervisor profile.
- Notification payloads contain only conversation UUID and highest committed sequence. They never
  contain event data.
- Multiple wakeups coalesce to the highest sequence. Every consumer reads event bodies from
  persistence, so missed, duplicated, or reordered notifications cannot create a stream gap.
- PostgreSQL consumers also reconcile on keepalive timeout, covering publisher/listener failure
  without introducing a second event transport.

Use fixed internal timing rather than new public settings: SQLite polls every 250 ms and both
backends emit/reconcile on a 15-second keepalive interval.

For `GET /api/v1/conversations/{conversation_id}/events`:

1. Authenticate and perform an owner-scoped conversation lookup before creating a subscription.
2. Parse `Last-Event-ID` as a non-negative conversation sequence; reject malformed values.
3. Subscribe to wakeups before reading replay state, closing the replay/live race.
4. Read committed events after the cursor, bounded to 5,000 events and 5 MiB, and compare the final
   replayed sequence to the transactionally observed high-water sequence. Calculate the byte cap
   from the UTF-8 `ConversationEvent` JSON used as SSE `data`, before framing.
5. If the complete range fits, emit each canonical event in sequence. If either bound is exceeded,
   or the requested cursor is ahead of the committed high-water sequence, discard partial replay
   and emit one fresh `snapshot` using `ConversationSnapshot` at its high-water sequence. Buffer
   the bounded replay decision before yielding so a stream never emits a partial replay followed
   by a snapshot.
6. Emit one `sync` frame at that same sequence before entering live delivery.
7. For every wakeup or reconciliation tick, replay strictly after `last_sent`; discard sequences at
   or below it. Apply the same caps and snapshot fallback to a large live backlog.
8. Emit comment keepalives without an `id`. On `CancelledError`, unsubscribe in `finally`, re-raise,
   and leave the command processor/runtime untouched.

Canonical frames use `id: <conversation sequence>`, `event: <event type>`, and the serialized
`ConversationEvent` as `data`. Snapshot and sync frames use their snapshot/high-water sequence as
`id`. Set `Content-Type: text/event-stream`, `Cache-Control: no-cache`, and
`X-Accel-Buffering: no`; document that reverse-proxy buffering/compression must be disabled for this
route.

## Work Package 5 — Django ASGI Lifecycle and Configuration

Add a small ASGI wrapper exported from `talktoharnesses.django.asgi`. The host composes it in its
own `asgi.py`:

```python
from django.core.asgi import get_asgi_application
from talktoharnesses.django.asgi import talktoharnesses_lifespan

application = talktoharnesses_lifespan(get_asgi_application())
```

The wrapper owns one service instance per process/event loop. On `lifespan.startup`, it constructs
the default Django persistence, registry, Grok factory, event broker, runtime manager, and service,
then awaits `service.start()`. It reports startup failure to the ASGI server rather than serving an
API with no worker. On `lifespan.shutdown`, it awaits the established service shutdown and reports
completion only after owned tasks/listeners are closed.

HTTP scopes pass unchanged to Django. A small process-local accessor lets API routers obtain the
already-started service and fails closed if the wrapper was not installed. `AppConfig.ready()` may
register system checks and model/admin metadata only; it must not create an event loop, connection,
worker, adapter, or subprocess.

The host owns Django settings, migrations, URL inclusion, and Uvicorn invocation. Document
`uvicorn host.asgi:application --host 127.0.0.1` as the package-provided/default exposure and warn
operators that authentication does not sandbox harness execution: authorized turn submitters cause
local programs to run as the Django OS user. Do not add a package CLI, auto-run migrations, or
modify host middleware/settings.

## Work Package 6 — Versioned Django-Ninja Surface

Ship one ready-made `NinjaAPI` and URLconf mounted by the host at `/api/v1`. Use API-level bearer
authentication, overridden with `auth=None` only for the four public surfaces. Keep route handlers
thin: parse/validate, derive `owner_id` from `request.auth`, call one facade method, and return its
Pydantic result.

The Phase 5 routes are:

| Method and path | Behavior |
| --- | --- |
| `GET /harnesses` | Keyset-page the caller's configured harnesses. |
| `POST /harnesses` | Create an owner-scoped named harness configuration. |
| `POST /harnesses/{id}/probe` | Run a fresh strict adapter probe and persist the successful result. |
| `GET /harnesses/{id}/capabilities` | Return the last successful probe projection. |
| `GET /harnesses/{id}/models` | Return models from that same capability projection. |
| `GET /harnesses/{id}/modes` | Return modes from that same capability projection. |
| `GET /conversations` | Page shells, excluding soft-deleted rows by default. |
| `POST /conversations` | Create an idle conversation bound to one owned harness. |
| `GET /conversations/search` | Run portable sanitized search and return shell pages. |
| `GET /conversations/{id}` | Return a sequence-stamped detail snapshot with 20 user-anchored turns. |
| `POST /conversations/{id}/archive` and `/unarchive` | Set or clear archival state. |
| `POST /conversations/{id}/pin` and `/unpin` | Set or clear `pinned_at`. |
| `POST /conversations/{id}/snooze` and `/unsnooze` | Set or clear `snoozed_until`. |
| `DELETE /conversations/{id}` | Soft-delete; never remove workspace files. |
| `GET /conversations/{id}/{turns,messages,tools,plans,activity}` | Return owner-scoped keyset pages. |
| `POST /conversations/{id}/turns` | Require `Idempotency-Key`; return command and target-turn projections. |
| `PATCH /conversations/{id}/queued-prompt` | Edit the currently editable queued prompt. |
| `DELETE /conversations/{id}/queued-prompt` | Cancel the queued prompt and settle its command. |
| `POST /conversations/{id}/steer` | Persist a steer-or-queue command through existing transitions. |
| `POST /conversations/{id}/interrupt` | Persist an interrupt command; return its projection. |
| `GET /conversations/{id}/interactions` | List pending/draft interactions. |
| `PATCH /conversations/{id}/interactions/{interaction_id}/draft` | Persist an editable draft. |
| `POST /conversations/{id}/interactions/{interaction_id}/resolve` | First-write-wins resolution and durable adapter-answer command. |
| `GET /conversations/{id}/events` | Replay/snapshot, sync, then live SSE. |
| `POST /auth/token/rotate` | Atomically replace the presented active token. |
| `POST /auth/token/revoke` | Revoke the presented token and return 204. |
| `GET /health` | Process liveness only. |
| `GET /ready` | Database, service-started, worker, and broker baseline readiness. |
| `GET /openapi.json` and `GET /docs` | Public generated API description/documentation. |

An absent or blank `Idempotency-Key` produces validation failure. Reusing a key with the same
submitted payload returns the original command and its current target turn without new events;
reusing it with a different payload returns `idempotency_conflict`.

Resource creation returns 201, command-producing turn/steer/interrupt/interaction routes return
202 with their durable projections, ordinary state mutations return their updated projection with
200, and successful revocation/soft-delete returns 204.

Map stable failures once at the Ninja API boundary:

- Generic token failure to 401.
- Owner-scoped missing/cross-owner/deleted resources to the same 404 response.
- Domain state, optimistic, interaction-resolution, and idempotency conflicts to 409.
- Invalid bodies, headers, cursors, and page limits to 422.
- Strict probe incompatibility to 409 with its stable domain code but no raw provider output.
- Unexpected failures to a generic 500 while logging only redacted diagnostics.

No handler performs global UUID lookup followed by an owner check. OpenAPI must mark every domain
route with bearer auth and only the explicitly public routes without it.

## Test Plan

### Facade and persistence contracts

- Run every facade read/mutation against SQLite and PostgreSQL repositories. Assert Python return
  objects against exact Pydantic projections, then reuse those expectations for HTTP JSON.
- Cover harness creation/probe, conversation creation, archive/pin/snooze/delete, each history page,
  interaction draft/resolution, and command projection updates.
- For every owner-scoped method and endpoint, create two users and assert that another user's UUID
  is indistinguishable from a missing UUID. Repeat for nested interaction and history IDs.
- Test cursor round trips, invalid cursors, equal sort keys, inserts between pages, sparse histories,
  20-user-turn snapshot boundaries, deleted rows, and deterministic ordering.
- Test portable search inclusion/exclusion and redaction. The same fixtures become Phase 8 search
  parity inputs.

### JWT and API tests

- Use the swappable test user model. Cover issuance, required claims, wrong algorithm/key,
  issuer/audience, expiry, malformed bearer headers, unknown/deleted/inactive users, stored-digest
  mismatch, token replacement, revocation, and generic identical 401 bodies.
- Race two rotations against one token and prove one succeeds. Race issuance/rotation/revocation
  around row creation and prove at most one token remains active.
- Verify startup rejects missing, short, or reused signing keys and that neither token nor raw
  `jti` appears in database rows, logs, errors, or OpenAPI examples.
- Assert route schemas/statuses, required idempotency headers, duplicate submission behavior,
  error mapping, and authentication metadata in OpenAPI.

### SSE and lifecycle tests

- Test replay below both caps, exactly at each cap, one event/byte over each cap, and a single event
  larger than the byte cap. Assert replay or one snapshot, then exactly one sync, then live events.
- Connect before, during, and after a commit; reconnect with `Last-Event-ID`; inject duplicated and
  coalesced wakeups; and concatenate streams to prove every committed event appears once in order.
- Test publisher failure, PostgreSQL notification loss/reorder, SQLite polling, simultaneous
  consumers, a slow consumer, keepalive reconciliation, and commit-before-publish.
- Disconnect SSE and ordinary HTTP clients while a turn runs and prove the durable worker reaches
  its terminal event. Assert subscriptions, listener connections, and async tasks are released.
- Exercise real ASGI lifespan startup failure, idempotent shutdown, and the existing ten-second
  runtime shutdown path. Confirm `AppConfig.ready()` starts nothing under `migrate`, `check`, and
  test discovery.

### End-to-end gate

- Start the packaged ASGI wrapper with the pinned Grok fixture peer and a file-backed SQLite
  database. Issue a token in-process, create/probe a harness and conversation through HTTP, open
  SSE, submit a turn, observe deltas and terminal state, interrupt a second turn, disconnect, and
  reconnect with `Last-Event-ID`.
- Repeat the persistence/SSE contract in the PostgreSQL CI job with two simultaneous subscribers.
- Gate with Ruff, format check, strict Pyright, full tests, lockfile check, migration drift check,
  wheel and sdist builds, and isolated installs proving core imports without Django/PyJWT.
- Phase gate: an authenticated client can run, observe, interrupt, reconnect to, and resume a Grok
  conversation without event gaps, duplicate committed events, cross-user disclosure, or request-
  owned harness work.

## Implementation Order

1. Reconcile the Phase 2/4 preconditions and freeze the shared output projections.
2. Add owner-scoped projection/search persistence operations and their common repository contract
   tests, then add the Django-only token store.
3. Implement `TalkToHarnessesService` and facade tests without Django-Ninja.
4. Implement JWT issuance/authentication and its database constraints.
5. Add the committed-event broker and test replay/live synchronization directly.
6. Add the ASGI wrapper, then mount thin Ninja routers over the tested facade.
7. Run cross-user, serialization-parity, SSE race, and end-to-end Grok gates before changing the
   package version to `2026.8.0.dev5`.

## Explicitly Out of Scope

- Harness switching and transcript handoff; these arrive atomically with Phase 8.
- Persistent approval rules or automatic approval decisions from Phase 6.
- Cursor, Codex, Claude Code, or OpenCode adapters from Phase 7.
- PostgreSQL FTS vectors, SQLite FTS5, retention cleanup, title recomputation, and enriched
  projection/index rules from Phase 8.
- Crash takeover, ambiguous delivery recovery, executable-change fallback, recent-probe readiness,
  OpenTelemetry, and fault injection from Phase 9.
- Cookies, Django session authentication, CSRF login, refresh tokens, multiple active tokens,
  asymmetric JWT algorithms, package-owned users, OAuth/OIDC, CORS policy, rate limiting, a CLI,
  WSGI streaming support, or a package-owned Uvicorn process.
