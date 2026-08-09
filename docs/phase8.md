# Phase 8 — Switching, Projections, Search, and Retention

## Summary

- Merge the completed Phase 7 branch first, then implement Phase 8 as version
  `2026.8.0.dev8`.
- Add durable harness switching through a transient candidate runtime. The current binding remains
  authoritative until the candidate has accepted the retained canonical handoff and the database
  commits the replacement binding.
- Keep one canonical handoff reader/renderer and reuse the same candidate-session path for
  post-retention session rotation. Do not add a second runtime manager or provider-specific handoff
  stores.
- Preserve the Phase 5 facade projections and metadata endpoints. Centralize the title rule and
  replace only the portable substring-search backend with PostgreSQL full-text search or SQLite
  FTS5.
- Add the externally scheduled `talktoharnesses_cleanup` Django management command. It removes
  expired database history, invalidates native sessions containing removed context, and never
  reads, changes, or deletes workspace files.
- Do not add crash takeover, ambiguous-delivery recovery, executable-change fallback,
  OpenTelemetry, new provider capabilities, or release documentation from Phases 9 and 10.

## Current Baseline and Entry Conditions

Phase 8 starts only after all Phase 7 adapter and live gates pass. Reconcile the merged checkout
before implementation:

- `Conversation.display_title` already expresses the required precedence of native, manual,
  derived, and fallback titles. `ConversationAggregate` and `ConversationShell` already contain the
  Phase 5 lightweight list fields, and archive, pin, snooze, and soft-delete are already functional.
  Phase 8 must reuse these contracts instead of creating richer parallel projections.
- `SwitchHarnessPayload`, `HarnessSwitchedPayload`, `HarnessSwitchFailedPayload`, `commit_switch()`,
  `fail_switch()`, `rotate_session()`, and `mark_requires_recreation()` already exist. The service,
  command processor, API route, binding history, and candidate-runtime orchestration do not.
- `SearchDocument` and the shared application-layer search-document builder already exist.
  `DjangoPersistence.search_conversations()` still performs the Phase 5 substring query, and
  `_delete_expired()` is intentionally a no-op.
- The active binding currently exists only inside aggregate JSON. Add relational binding history as
  Phase 8 work because atomic replacement, switch-back behavior, and closing the previous binding
  cannot be implemented correctly without it.
- The roadmap's Phase 2 schema includes raw native events, chunks, and file-change rows, but this
  checkout does not persist all of them. Reconcile that prerequisite before the Phase 8 migration.
  Do not create unused tables in Phase 8 merely to satisfy a checklist; every actually persisted
  turn-owned table must have a tested deletion path.
- The default production registry must contain all five completed Phase 7 adapters. Extract its
  construction only when both ASGI composition and the cleanup command need it.

The database-specific search design follows the supported engines rather than introducing a search
service. PostgreSQL supports stored `tsvector` documents queried with `plainto_tsquery` and indexed
with GIN ([PostgreSQL text-search controls](https://www.postgresql.org/docs/current/textsearch-controls.html),
[GIN indexes](https://www.postgresql.org/docs/current/gin.html)). SQLite FTS5 provides virtual
tables and `MATCH`; its external-content mode requires the application to keep the content table and
index synchronized, so this phase uses explicit migration-created triggers and a backfill
([SQLite FTS5](https://www.sqlite.org/fts5.html#external_content_and_contentless_tables)). The
cleanup entry point is an ordinary Django `BaseCommand`, discovered from the app's
`management/commands` package
([Django custom management commands](https://docs.djangoproject.com/en/5.2/howto/custom-management-commands/)).

## Public Contracts

### Python facade

Add one asynchronous facade method:

```python
async def switch_harness(
    owner_id: str,
    conversation_id: UUID,
    *,
    harness_id: UUID,
    idempotency_key: str,
) -> CommandProjection: ...
```

The method owner-scopes both the conversation and target harness before accepting a durable
`SWITCH_HARNESS` command. `Idempotency-Key` is required because retries must not create multiple
candidate sessions or bindings. Reusing a key with a different target is
`idempotency_conflict`; an exact retry returns the original command projection.

The switch command is accepted only when no turn is active or queued and no background activity is
running. The worker rechecks those conditions immediately before candidate creation. A command
claimed while the conversation is still finishing is released back to `accepted`, without a
candidate session, until the conversation becomes eligible.

Do not add a synchronous switch API, expose the handoff document, or return provider-native session
identifiers.

### HTTP API

Add the request-only model:

```python
class SwitchHarnessBody(BaseModel):
    harness_id: UUID
```

Add `POST /api/v1/conversations/{conversation_id}/switch`, require `Idempotency-Key`, and return the
durable `CommandProjection` with status 202. Keep the handler thin: owner scoping, target resolution,
idempotency checks, and state validation remain in `TalkToHarnessesService`.

The existing list, search, detail, archive, pin, snooze, soft-delete, history, and SSE response
models remain unchanged. `harness_switched`, `harness_switch_failed`, title, and session-rotation
events continue to use the existing `ConversationEvent` union.

### Internal persistence operations

Extend `Persistence` with coarse operations, not generic binding or retention CRUD:

- Read an ordered retained canonical handoff for an owner-scoped or worker-scoped conversation.
- Atomically commit a successful switch: close the old binding row, insert the new active binding,
  update aggregate state and shell fields, persist the accepted candidate process/launch record,
  settle the command, and insert `harness_switched`.
- Atomically commit a failed switch against the unchanged current binding, settle the command, and
  insert only `harness_switch_failed` from the attempted switch.
- Prune eligible history in short per-conversation transactions and return the conversations whose
  native sessions must be rotated.
- Commit successful retention rotation or mark the binding as requiring recreation after a failed
  rotation.
- Permanently purge soft-deleted aggregates older than the cutoff.

Replace the two Phase 1 retention placeholders with operations that can express these transactions;
do not retain a misleading `delete_expired_turn_aggregates()` API whose Django implementation can
only return zero.

## Work Package 1 — Relational Ownership, Ordering, and Title Projection

### Phase 8 migration

Create one migration after Phase 7 with these minimal schema changes:

- Add a private `ConversationBindingRecord` containing the fields already present in
  `ConversationHarnessBinding`: binding ID, conversation FK, kind, configuration, source harness
  instance ID, native session ID, launch snapshot, recreation flag, active flag, creation time, and
  close time. Add a conditional unique constraint allowing one active binding per conversation and
  an index on `(conversation, -created_at)`.
- Backfill the current binding from each aggregate's validated state JSON. The data migration must
  fail on an invalid binding rather than silently inventing history.
- Add `order_index` to `MessageRecord`, populated from the first canonical event that created the
  message. Retain the existing message-local `sequence`; it is chunk order, not conversation order.
- Add a nullable indexed `turn_id` to `ConversationEventRecord` and a nullable indexed
  `target_turn_id` to `CommandRecord`. Populate them from validated event/command payloads. Resolve
  interaction- and activity-only events through their existing projection rows during backfill.
- Ensure every restored raw-event, chunk, file-change, or provider-event row from the entry
  reconciliation has a turn FK or indexed turn ID. Child projections with real `TurnRecord` foreign
  keys continue to rely on database cascades.

Do not expose `ConversationBindingRecord` as a public model. The aggregate's
`conversation.current_binding_id` and active `binding` remain the domain source used by transitions;
the relational rows provide transactional history and deletion/query integrity.

### One title rule

Keep `Conversation.display_title` as the single precedence rule:

1. Non-empty native title.
2. Non-empty preserved manual title.
3. Derived title.
4. `Untitled conversation`.

Add one pure helper that derives at most the first eight whitespace-delimited words of the earliest
retained user message. It collapses whitespace and returns `None` for empty text. Do not add language
detection, summarization, punctuation rewriting, or a background title job.

Call the helper from the existing projection materialization transaction after message rows are
updated, and from retention after rows are pruned. Store `title_derived` in aggregate JSON and copy
`display_title` to `ConversationAggregate.title`; the list shell, detail projection, search document,
and SSE snapshot then all observe the same value. Native-title events and manual titles continue to
update their existing source fields and are never rewritten by derivation.

### Existing lightweight projections

Retain the existing `ConversationAggregate` shell columns and keyset order
`(updated_at DESC, conversation_id DESC)`. Audit their update paths so switch, retention, and title
recomputation refresh `title`, `harness_kind`, `model`, `mode`, pending-interaction state, and search
content in the same transaction. Do not create a second list table or new public projection merely
because Phase 8's milestone repeats archive, pin, snooze, and soft-delete.

## Work Package 2 — Canonical Handoff and Candidate Runtimes

### Ordered handoff

Add one internal immutable handoff representation and one renderer in the application layer. The
persistence read merges rows by `(turn.order_index, item.order_index, stable UUID)` and emits only:

- retained user and assistant message text, role, and interrupted state; and
- canonical tool name, normalized arguments, outcome, exit status, paths, and the existing UTF-8-safe
  2 KiB `output_tail`.

Use the existing `CanonicalToolResult` validation for the tail limit. Do not read or render
reasoning, plans, usage, raw/native events, stderr, `full_output`, provider summaries, generated tool
summaries, approval metadata, or deleted rows. The renderer produces one deterministic text prompt
from these typed entries; adapters do not each invent a transcript format.

The handoff reader is also the sole retained-context source for retention rotation. This guarantees
that switching and rotation cannot disagree about what context is allowed to reach a new native
session.

### Runtime-manager seam

Refactor only the launch/start mechanics already shared by normal and candidate sessions:

- Add a transient candidate path to `RuntimeManager` that probes, validates paths, starts the
  process or SDK context, creates a new native session, and returns a `ManagedRuntime`-compatible
  handle without inserting it into the live conversation map or writing lifecycle rows.
- Candidate startup uses a new binding UUID supplied by the caller and counts against the existing
  runtime capacity. It never reads the current binding from persistence.
- Seed a non-empty handoff through the existing `HarnessAdapter.submit()` contract with private
  synthetic turn/command UUIDs. Drain and validate candidate events until the authoritative terminal
  event for that synthetic turn. Discard candidate assistant/reasoning/tool/usage events; never
  materialize or publish them. A provider interaction, failed/unknown terminal result, wrong turn,
  timeout, or ended stream rejects the candidate.
- An empty handoff needs only successful native-session creation; do not send an empty synthetic
  turn.
- Add explicit `promote_candidate()` and `close_candidate()` paths. Promotion occurs only after the
  database switch commit and installs the already-started candidate as the conversation's one live
  runtime. Candidate close uses the existing adapter/process shutdown deadlines.

`HarnessAdapter`, `HarnessSession`, and provider event schemas remain unchanged. Do not add a public
`seed_transcript()` method, a provider-specific persistence hook, or another event pump.

### Stale-runtime defense

Before reusing a managed runtime or committing one of its events, compare its session binding ID to
the aggregate's current binding ID and recreation flag. Close a mismatch and start/resume from the
authoritative binding. This narrow check is required because a separately scheduled cleanup command
can invalidate a session while an ASGI process still has an idle or waiting runtime in memory. A
stale event may fail optimistic concurrency, but it must never recreate a deleted turn or restore an
old native session.

## Work Package 3 — Durable Harness Switching

### Command acceptance

`TalkToHarnessesService.switch_harness()` performs these steps only:

1. Owner-scope the conversation and target `HarnessRecord`; cross-owner IDs equal missing IDs.
2. Require an idle conversation with no queued turn or running background activity.
3. Validate the target's last successful probe and requested finite model/mode using the same
   configuration checks as normal session start.
4. Create one `SWITCH_HARNESS` command using the existing payload, persist it through
   `commit_facade_mutation()`, and return its projection.

The payload stores the resolved target configuration plus its harness instance ID if that source
field is added to the existing payload; do not trust a worker-time request body or allow arbitrary
configuration in the route. An exact idempotent retry does not probe again or create a candidate.

### Worker transaction

Handle `SWITCH_HARNESS` before the command processor's normal `_ensure_runtime()` path so a reaped
old session is not resumed merely to replace it:

1. Reload and lock the aggregate through the switch-specific persistence operation. If a turn,
   queued prompt, or background activity appeared, release the command to `accepted` and stop.
2. Read and render the retained handoff from the same committed version.
3. Create a fresh binding UUID and transient candidate session for the target. Never resume a
   historical binding, including when switching back to a previously used harness kind.
4. Seed/drain the handoff. Keep the current runtime and binding untouched while this occurs.
5. Quiesce the current event pump and flush its delta batch. Atomically apply `commit_switch()`,
   persist binding/process/launch rows, update shell/search projections, mark the switch command
   delivered and settled, and commit `harness_switched`.
6. Publish the committed event, promote the candidate, then close the previous runtime/session.
   Failure to close the already-replaced old runtime is logged and force-terminated through the
   existing supervisor; it does not roll back the committed binding.

If any step before commit fails, close the candidate, restart the unchanged old event pump when it
was quiesced, apply `fail_switch()` to the latest old-binding state, settle the command with the
sanitized error code/message, and publish only `harness_switch_failed`. A one-shot database failure
after candidate creation follows this same compensating path. If persistence remains unavailable,
the non-negotiable invariant is candidate closure and no in-memory promotion; Phase 9 owns general
ambiguous crash recovery.

The switch path must not copy approval rules, mutate workspace files, delete provider sessions,
reuse native dedupe IDs, or carry native session IDs across bindings. The promoted adapter starts
with an empty native-ID/offset dedupe set for its new native session.

## Work Package 4 — PostgreSQL and SQLite Full-Text Search

### Shared document and query semantics

Keep `application.search_documents` as the only inclusion/sanitization rule. It continues to build
normalized text from the effective title, retained user/assistant messages, and canonical tool name,
arguments, paths, and output tail. Retention and every projection commit rebuild this row in the
same database transaction. Deleted reasoning, raw events, stderr, secrets removed by the central
redactor, and full tool output never reach it.

Use one literal lexical normalizer for both documents and queries: case-fold, replace non-alphanumeric
characters with spaces, collapse whitespace, and retain the resulting non-empty terms. Search joins
query terms with AND; provider-specific Boolean, prefix, column, phrase, and ranking syntax is not
public. Empty normalized input returns an empty page. Results retain the Phase 5 list order and
cursor semantics rather than relevance order. Using the same term stream on both backends is the
parity rule; do not attempt to make two independent query parsers behave alike.

### PostgreSQL backend

In the vendor-aware migration, add a stored `tsvector` column derived from
`to_tsvector('simple', coalesce(normalized_text, ''))` and create a GIN index on it. Query it with
`@@ plainto_tsquery('simple', %s)` using a bound parameter. Keep owner, soft-delete, archive filter,
cursor, and limit predicates on `ConversationAggregate`; the search index only supplies matching
conversation IDs.

Keep the PostgreSQL-only column and SQL private to `DjangoPersistence`. Do not make core or a
Django+SQLite install import `django.contrib.postgres`, Psycopg, or a PostgreSQL field class.

### SQLite backend

In the same vendor-aware migration, require FTS5 and create a private virtual table containing
`conversation_id UNINDEXED` and `normalized_text` with the `unicode61` tokenizer. Create insert,
update, and delete triggers on `talktoharnesses_search_document`, then backfill existing documents.
The FTS table is a derived index; `SearchDocument.normalized_text` remains the content source and
the shared builder remains the knowledge source.

Compile the literal terms into a bound FTS5 `MATCH` expression with each token quoted and joined by
`AND`. Join matching conversation IDs back to owner-scoped, non-deleted aggregates and apply the
same ordering/cursor rules as PostgreSQL. Migration fails clearly when the deployed SQLite lacks
FTS5; do not silently fall back to the Phase 5 table scan.

Migration reversal drops only the vendor-specific triggers, virtual table, generated vector, and
GIN index. It preserves `SearchDocument` rows so rollback does not destroy canonical sanitized
search text.

## Work Package 5 — Six-Month Retention Command

### Cutoff and eligibility

Add one pure UTC helper for “six calendar months before now.” Subtract six from the year/month pair,
preserve the time and UTC offset, and clamp the day to the final day of the target month. Do not add
`python-dateutil` for this one calculation.

On each run, `talktoharnesses_cleanup` uses one captured UTC `now` and cutoff. It processes short,
locked per-conversation transactions:

- Permanently purge a soft-deleted aggregate when `deleted_at <= cutoff`. Existing cascade rules
  remove conversation-owned rows and both search indexes. Immutable Phase 6 interaction audits are
  copied records without an aggregate FK and remain retained.
- Skip history pruning for a conversation with a running turn or any running background activity.
- Delete terminal turns whose `completed_at <= cutoff`.
- For an active `WAITING` turn whose start/creation time is at or before the cutoff, reuse
  `cancel_open_interactions()` and `interrupt_turn(reason="retention")`, settle its command, then
  prune that complete turn aggregate. Do not release a new answer command to a provider session that
  is being invalidated.
- Leave queued and non-expired turns unchanged.

Use `<=` consistently so a boundary row is handled once. A rerun with the same cutoff is idempotent.
The command accepts no path, retention-period, provider, or deletion-scope options.

### Complete turn deletion

For each selected turn, delete all mutable conversation history owned by it:

- messages and message chunks;
- reasoning blocks;
- plans;
- canonical tools, tool chunks, full retained tool output, file changes, and usage;
- interactions, drafts, provider correlation, and interaction answers;
- background activity belonging to the turn after confirming none is running;
- durable commands targeting the turn, including coalesced commands;
- canonical and redacted raw/native events linked to the turn; and
- the `TurnRecord` itself.

Remove the same commands, interactions, answers, and activities from aggregate JSON. Preserve
conversation-level metadata, binding/launch history, approval audits, events unrelated to a deleted
turn, the monotonic next event sequence, and workspace files. Sequence gaps left by deleted events
are valid; new events never reuse them.

After deletion, recompute the derived/effective title, shell fields, pending-interaction flag, and
the shared search document in the same transaction. Return the retained handoff plus the previous
native session identity needed for rotation.

### Native-session rotation

Pruning applies the existing `rotate_session()` transition in the same transaction that clears the
active binding's `native_session_id` and native dedupe sets. Publish that committed
`session_rotated` event immediately; it records invalidation of the old session, so deleted context
is never resumable even if later provider work fails. Then:

1. If no retained handoff remains, leave the binding ready for ordinary lazy creation.
2. Otherwise use the Work Package 2 candidate path with the same active binding/configuration to
   create and seed a replacement native session from retained content only.
3. On acceptance, commit the new native session ID and launch snapshot, clear
   `requires_session_recreation`, and close the transient runtime while retaining its resume
   identity.
4. On candidate failure, keep the already-cleared native ID, apply `mark_requires_recreation()`,
   and continue cleanup. History deletion succeeds even though rotation did not.

The next command must create a fresh session when the flag is set and may clear it only after
successful creation. It must never fall back to the pre-pruning native ID.

### Command composition and output

Place the Django entry point at
`talktoharnesses/django/management/commands/talktoharnesses_cleanup.py`. Keep retention orchestration
in a small application-layer async function so it can be tested with the memory persistence and fake
runtime; the `BaseCommand` only builds the normal Django composition, invokes it, and prints counts
for purged conversations, pruned turns, cancelled waiting turns, successful rotations, and bindings
requiring recreation.

Share the fixed default adapter-registry constructor with ASGI composition. Do not start command
workers, expose the cleanup function over HTTP, add an internal scheduler, or start work from
`AppConfig.ready()`.

## Tests and Phase Gate

### Domain and application tests

- Table-test title precedence, eight-word derivation, whitespace handling, empty messages, and
  recomputation when the earliest retained user turn is deleted.
- Test handoff ordering across interleaved messages/tools and multiple turns. Assert exact inclusion
  of canonical fields and exclusion of reasoning, plans, raw events, full output, deleted content,
  and generated summaries. Reuse the existing UTF-8 tail boundary cases.
- Test candidate start, empty handoff, successful seed drain, terminal-without-message, transcript
  rejection, interaction during seed, timeout, close, promotion, and capacity accounting for
  process-bound and SDK-managed fake adapters.
- Test switch acceptance/idempotency, owner isolation, busy/deferred execution, success, failure,
  switching back, fresh native/dedupe identity, old-runtime close, candidate close, and a one-shot DB
  failure after candidate creation.

### Persistence and search tests

- Run the binding-history, atomic switch, handoff-read, retention, and title contract against SQLite
  and PostgreSQL. Assert one active binding under races and unchanged current binding after every
  failed-switch injection point.
- Run identical search fixtures on both databases for casing, punctuation, multiple terms, title,
  user/assistant messages, normalized tool fields/tails, owner isolation, soft deletion, archive
  filters, keyset pagination, and removed-content disappearance. Assert result IDs and order, not
  backend-specific rank values.
- Verify migration backfill, reverse migration, SQLite trigger synchronization/FTS integrity, and a
  PostgreSQL query plan that can use the GIN index on a non-trivial fixture corpus.

### Retention tests

- Cover month-end and leap-year cutoff calculation plus exact-boundary rows.
- Cover completed, failed, interrupted, outcome-unknown, waiting, running, queued, and
  background-active states. Verify waiting interactions are cancelled, running/background-active
  conversations are untouched, and reruns are idempotent.
- Seed every persisted turn-owned table and prove complete deletion while conversation metadata,
  binding history, approval audits, unrelated events, and sequence high-water remain intact.
- Verify search/title recomputation, successful retained-context rotation, empty-history rotation,
  candidate failure with `requires_session_recreation`, stale-runtime rejection, soft-delete purge,
  and that fixture workspace files are byte-for-byte unchanged.
- Invoke `talktoharnesses_cleanup` through Django's command test helper on SQLite and in the dedicated
  PostgreSQL CI job.

### End-to-end and release gate

1. Create a conversation on fake harness A, persist multiple canonical turns/tools, switch to B,
   submit a new turn, switch back to A, and prove the second A binding has a new native session while
   all public history remains one canonical conversation.
2. Repeat with candidate transcript rejection and DB failure; the original binding/runtime must
   continue accepting turns and only `harness_switch_failed` may describe the attempt.
3. Age one retained turn and one waiting turn, run cleanup, reconnect SSE, and prove snapshots,
   replay, search, and the next resumed turn contain no deleted context.
4. Run Ruff, format check, strict Pyright, the full unit/property/contract/e2e suite, SQLite and
   PostgreSQL jobs, migration drift, lockfile check, wheel/sdist builds, isolated core imports, and
   deterministic support-document regeneration.
5. Change the package version to `2026.8.0.dev8` only after all gates pass.

The Phase 8 gate passes when switching can never replace or close the current binding before a
candidate is durable, PostgreSQL and SQLite return the same ordered search results, and neither an
old binding nor a future runtime can resume native context removed by retention.

## Implementation Order

1. Reconcile the Phase 7 registry and missing Phase 2/4 turn-owned persistence prerequisites.
2. Add binding history, message/event/command ordering links, migration backfills, and common
   persistence contracts.
3. Centralize derived-title calculation and make all existing shell/search projection updates use
   it.
4. Implement and test the canonical handoff reader/renderer.
5. Extract the candidate-runtime seam, then use it for durable switch commands and the API route.
6. Add PostgreSQL vector/GIN and SQLite FTS5 migrations and backend-specific query implementations.
7. Implement transactional pruning, reuse the candidate path for rotation, and add the management
   command.
8. Run parity, failure-injection, retention-completeness, and end-to-end gates before updating the
   development version.

## Explicitly Out of Scope

- Phase 9 worker takeover, ambiguous command replay, eager startup recovery, executable-change
  fallback, recent-probe readiness, generalized SSE/process recovery, fault injection framework,
  and OpenTelemetry.
- Phase 10 stabilization, final compatibility matrix expansion, deployment guide, performance
  targets, public API audit, and stable release version.
- Phase 11 search/retention/transcript/surface product extensions: ranking and query operators,
  configurable retention and workspace/provider cleanup, public handoff or native-session exposure,
  second list projections, and further `HarnessAdapter` / provider-capability changes. See
  `docs/implementation-plan.md` Phase 11.

## Assumptions

- A switch targets an existing owner-scoped harness configuration. Every successful request creates
  a new binding/native session; switching back never resumes the binding that was active before the
  intervening switch.
- Text remains the only handoff input supported by all five Phase 7 adapters. Provider-native
  history import is not assumed.
- Canonical retained rows are already redacted at their persistence boundary. Phase 8 controls which
  retained fields are indexed or handed off; it does not add a second secret detector.
- The externally scheduled cleanup command is run as the same OS/Django principal and with the same
  provider dependencies/authenticated state as normal service composition. Concurrent stale
  runtimes are rejected through binding/version checks rather than trusted to notice cleanup.
- SQLite deployments must provide FTS5. PostgreSQL deployments install the existing `postgres`
  extra; core and Django+SQLite installations remain Psycopg-free.
