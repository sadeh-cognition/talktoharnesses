# `talktoharnesses` phased implementation plan

## Summary

- Build on the existing clean Phase 0 package skeleton and accepted ADRs.
- Implement phases sequentially; each phase is a separate mergeable increment with applicable tests, Ruff, formatting, Pyright, packaging, and migrations green. Versions remain `*.devN` until Phase 10.
- Phase 7 is split into independently mergeable 7A–7D adapter increments.
- Keep one distribution with feature extras: `django`, `postgres`, `grok`, `cursor`, `codex`, `claude`, `opencode`, `otel`, and `all`. Grok/Cursor extras may contain no Python runtime dependency because their adapters supervise external executables.
- Core imports remain Django-free. All execution paths require an injected persistence implementation.
- Do not add temporary public stubs, plugin discovery, synchronous wrappers, or speculative abstractions.

## Phase 1 — Domain model and adapter contracts

### Implementation

- Define frozen Pydantic models with strict validation and discriminated unions. Use UUID-backed identifiers, timezone-aware UTC timestamps, explicit enums, and `extra="forbid"`.
- Model harness configuration/capabilities, conversations, bindings, turns, messages, reasoning, plans, tools, files, commands, usage/cost, interactions, activities, processes, errors, and API projections.
- Define one canonical event envelope containing event ID, conversation ID, conversation-local sequence, timestamp, event type, and typed payload. Implement every event family listed in the requirements.
- Implement pure transition functions for:

  - One active turn per conversation.
  - Submission, coalesced queued prompts, edit, cancellation, and start.
  - Steer-or-queue with automatic queue fallback.
  - Terminal completion independent of a final assistant message.
  - Background activity after parent-turn completion.
  - Interaction draft/submission and first-write-wins resolution.
  - Interrupt, failure, and `outcome_unknown`.
  - Session reaping, rotation, switching, and recovery states.

- Define coarse asynchronous persistence protocols around business operations rather than generic CRUD: conversation snapshots, command acceptance/claims, event-batch commits, interaction resolution, replay, and retention.
- Define `CommittedEventPublisher`; only already-committed events may be passed to it.
- Implement the fixed adapter registry keyed by `HarnessKind`. Reject duplicate or missing registrations.
- Define a versioned transcript-fixture format containing probe output, launch metadata, ordered native input/output, expected canonical events, and redaction assertions.

### Public contracts

- Export canonical models and errors from `talktoharnesses.domain`.
- Export `HarnessAdapter`, repository protocols, publisher protocols, and registry types from the application/provider packages.
- Preserve the adapter methods from the supplied contract exactly; provider-specific types must not leak through them.
- Use canonical error codes such as `persistence_required`, `conversation_busy`, `mode_change_while_active`, `unsupported_native_event`, and the two missing-path errors.

### Tests and gate

- Table-test every legal and illegal state transition.
- Use Hypothesis for event-ordering, one-active-turn, interaction-resolution, and queued-prompt invariants.
- Verify all Pydantic unions reject unknown variants and fields.
- Verify registry isolation and transcript-fixture round trips.
- Gate: every orchestration invariant is executable without Django or a real harness.

## Phase 2 — Django persistence and durable commands

### Implementation

- Add the complete initial ORM schema so later phases add behavior rather than repeatedly reshaping core data:

  - Owner-scoped harnesses, probes, conversations, bindings, turns, and commands.
  - Transcript, message chunks, reasoning, plans, tool results/chunks, file changes, and usage.
  - Interactions, answers, approval rules/audits, and background activities.
  - Conversation/raw events, process records, launch snapshots, and token state.

- Use the swappable Django user model for ownership. Every owner-bearing table must have an owner path that can be enforced without trusting request-supplied IDs.
- Add database constraints for one active turn, idempotency keys, one submitted interaction answer, conversation sequence uniqueness, message/tool chunk sequence uniqueness, and native-ID deduplication.
- Allocate event sequences by locking/updating the conversation’s `next_event_sequence` in the same transaction as projection updates and event insertion. Increment the optimistic `version` in that transaction.
- Keep the repository API asynchronous. Execute Django transaction blocks through a thread-sensitive async bridge because Django transactions remain synchronous boundaries.
- Implement command states: `accepted`, `claimed`, `delivery_started`, `delivered`, `settled`, `coalesced`, and `outcome_unknown`. Store worker ID, lease expiry, attempts, delivery timestamps, native session ID, and recovery result.
- For duplicate idempotency keys, return the original command and its current target turn. A steer command keeps stable command identity; only the documented steer-failure fallback may retarget it to a queued turn.
- Join queued submissions transactionally into one editable user message using newline separators. Later command rows become `coalesced` and reference the executable command and same queued turn.
- Implement renewable claims:

  - PostgreSQL uses `SELECT … FOR UPDATE SKIP LOCKED`.
  - SQLite permits one supervisor and serializes claims through its write transaction.
  - Enforce the 20-runtime limit while claiming execution work; excess work stays queued.

- Implement the 50 ms delta accumulator. Each flush atomically writes chunk rows, materialized state, canonical events, and dedupe offsets; publication happens after commit.
- Add centralized structured/text redaction before raw events, stderr, errors, or payloads cross a persistence boundary.
- Add `[postgres]` with Psycopg 3 and a dedicated Ubuntu PostgreSQL CI job. Keep SQLite tests in the existing OS matrix.

### Public contracts

- Provide `DjangoPersistence` as the production implementation of the Phase 1 protocols.
- Do not expose ORM models as public Python API types.
- Expose committed replay by `(conversation_id, after_sequence, event_count_limit, byte_limit)`.

### Tests and gate

- Run the same repository contract suite against SQLite and PostgreSQL.
- Test duplicate idempotency keys, concurrent sequence allocation, optimistic conflicts, lease expiry/renewal, coalescing, and first-write-wins constraints.
- Crash the batcher before and after commit and verify that only uncommitted deltas can disappear.
- Verify committed events replay byte-for-byte in sequence after repository reconstruction.
- Verify core imports still succeed without Django installed.

## Phase 3 — Process and runtime supervision

### Implementation

- Define a Django-independent supervisor around `ProcessSpec`, `ProcessHandle`, `ProcessEvent`, and immutable `LaunchSnapshot`.
- Resolve executable symlinks, require a regular executable file, and verify ownership by the current Django OS user before launch.
- Validate the primary working directory and all additional roots without creating anything.
- Launch argument arrays directly without a shell. Adapters exclusively construct arguments; configurations expose only an executable path where supported.
- On Unix, create a new process session/group. On Windows, use a Job Object with kill-on-close and the appropriate new-process-group flags.
- Reserve stdout exclusively for protocol frames. Capture stderr separately, redact it, retain only the newest 10 MiB, and emit one truncation event per process incarnation.
- Implement creation, session-start/resume, idle-reap, silence-warning, interrupt, graceful close, termination, and forced descendant-tree termination timers.
- Implement `RuntimeManager` with one runtime per conversation and no shared adapter/SDK objects between conversations.
- Idle reaping closes the process/runtime but retains the native resume identifier and launch history. Live background activity suppresses reaping.
- On shutdown, interrupt active work, allow up to ten seconds, then terminate descendants. Do not interpret provider silence as failure.

### Tests and gate

- Use deterministic fake child processes for malformed stdout, large stderr, silence, startup hangs, ignored interrupts, descendant creation, and abnormal exits.
- Verify process-tree termination on Linux, macOS, and Windows CI.
- Verify exactly one truncation event, strict stdout separation, path errors, ownership rejection, and immutable launch snapshots.
- Gate: the supervisor can repeatedly start, interrupt, reap, and close isolated fake runtimes without leaked children or tasks.

## Phase 4 — Grok vertical slice and ACP foundation

### Implementation

- Implement the minimum shared ACP/JSON-RPC layer required by Grok and Cursor:

  - Newline-delimited stdio framing.
  - Request ID correlation and cancellation.
  - Bidirectional requests and notifications.
  - Capability negotiation.
  - Strict Pydantic decoding for the allowlisted tested schema.
  - Clean shutdown and pending-request failure.
  - Separate provider-extension schemas.

- Do not implement the entire ACP specification or pass unknown events through. Unknown methods, fields, requests, or notifications produce `unsupported_native_event`.
- Launch Grok using its local ACP stdio mode without `--always-approve`, because package-level approvals must remain observable. Grok officially documents long-lived ACP over strict stdio JSON-RPC, including sessions, streaming, reasoning, tools, plans, and permission prompts. [Grok ACP documentation](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md)
- Implement probe, strict version matching, session create/load, prompt, steer, cancel, permissions, structured questions, plans, tools, commands, files, reasoning, usage, title, and nested activity normalization.
- Record full redacted normalized tool input/output during retention. Derive a separate UTF-8-safe 2 KiB output tail for `CanonicalToolResult` handoffs.
- Connect adapter events to the durable command processor:

  1. Accept and commit command.
  2. Claim and record `delivery_started`.
  3. Invoke the adapter.
  4. Persist normalized state/events.
  5. Publish committed events.
  6. Settle the command on the authoritative terminal signal.

- Add the first strict compatibility entry with separate create/resume matrices. Generate a provisional Grok section in `SUPPORTED_HARNESSES.md`.
- Implement lazy Grok resume after idle reaping and minimal startup recovery sufficient for a clean service restart. General ambiguous recovery remains Phase 9.

### Tests and gate

- Replay fixtures for every supported Grok native message and error path.
- Test split/coalesced protocol frames, duplicate IDs/offsets, permission races, steering, interrupt, no-final-message completion, background activity, and unknown events.
- Add opt-in real-Grok tests. A configured but untested version must fail, not skip.
- Gate: a persisted Grok conversation survives idle reaping and a clean service restart, while malformed/unknown native input only closes that conversation.

## Phase 5 — Python facade, Django-Ninja API, JWT, and SSE

### Implementation

- Introduce `TalkToHarnessesService` as the asynchronous Python facade and explicit async lifecycle owner. It receives persistence, registry, publisher, clock, and runtime manager dependencies.
- Provide a Django ASGI lifespan wrapper that starts command workers and shuts them down cleanly. Do not start background workers from `AppConfig.ready()`.
- The host application remains responsible for its Django settings and Uvicorn invocation. Document `127.0.0.1` as the package-provided/default serving configuration; no CLI is added.
- Add JWT authentication using HS256 and a required dedicated signing key. Implement:

  - Trusted in-process `issue_token(user)`.
  - Authenticated HTTP rotation and revocation.
  - Thirty-day configurable expiry.
  - Hashed `jti` storage and one active token per user.
  - Issuer/audience validation.
  - Generic authentication failures.
  - Rejection of inactive Django users on each new request.

- Mount a versioned Django-Ninja surface under `/api/v1`:

  - `/harnesses` and `/{id}/probe`, capabilities, models, and modes.
  - `/conversations`, `/{id}`, archive, pin, snooze, soft delete, and search.
  - Paginated turns, messages, tools, plans, and activity.
  - Turn submission, queued-prompt edit/cancel, steer, interrupt, and switch.
  - Pending-interaction list, draft update, and resolution.
  - `/auth/token/rotate` and `/auth/token/revoke`.
  - `/{conversation_id}/events` for SSE.
  - Unauthenticated `/health`, `/ready`, `/openapi.json`, and documentation.

- Require `Idempotency-Key` on turn-submission requests. Return the durable command projection and current target-turn projection.
- Scope every service operation by the authenticated user before looking up globally unique conversation IDs.
- Use one Pydantic projection for Python return values, JSON responses, snapshots, and SSE data.
- Implement unsigned but opaque URL-safe keyset cursors containing sort key and UUID; cursor structure is not a public contract.
- Implement lightweight conversation shells and detail snapshots with the latest 20 user-anchored turns by default.
- Implement a correct portable search baseline over sanitized search-document rows. Phase 8 replaces only its query backend with PostgreSQL/FTS5.
- Implement SSE replay and live delivery:

  - Use event sequence as `id`.
  - Honor `Last-Event-ID`.
  - Replay no more than 5,000 events or 5 MiB.
  - Send a fresh snapshot when either cap is exceeded.
  - Emit `sync` after replay/snapshot and before live events.
  - Deduplicate live wakeups by sequence.
  - Use PostgreSQL notifications after commit and SQLite polling.

### Tests and gate

- Contract-test Python and HTTP serialization against the same expected Pydantic output.
- Test token issuance, invalidation, rotation races, revocation, expiry, inactive users, and generic failures.
- Test every owner-scoped endpoint against cross-user IDs.
- Test cursor pagination, sparse histories, replay caps, reconnect races, commit-before-publish, and simultaneous SSE consumers.
- Gate: an authenticated client can run, observe, interrupt, reconnect to, and resume a Grok conversation without event gaps or duplicate committed events.

## Phase 6 — Persistent approvals and complete interactions

### Implementation

- Add one provider-neutral interaction broker used by every adapter. It persists the request before waiting for any answer.
- Support multiple concurrent interactions, editable answer drafts, immutable submission, indefinite waiting, and transactional first-write-wins resolution.
- Implement immediate decisions `allow_once`, `allow_session`, `deny`, and `cancel`.
- Implement persistent rules with explicit normalized match types:

  - Exact command argument array.
  - Resolved filesystem path plus read/create/modify/delete operation.
  - Recursive directory matching.
  - Blanket network access.
  - Scope to conversation, harness instance, executable, user, or principal-global.

- Evaluate matching rules by specificity only to collect candidates; any matching deny wins. Otherwise a matching allow resolves the request. No rule mutates after use.
- “Create rule and allow” is a single transaction that stores the rule, immutable audit snapshot, request resolution, and canonical events before answering the harness.
- Hard deletion removes only the live rule. Audit records retain copied scope, matcher, decision, principal, timestamps, and provider request identifiers.
- Automatic decisions emit the same request and resolution events as manual decisions.
- Add approval-rule CRUD and audit projections to both the Python service and authenticated HTTP API.

### Tests and gate

- Test exact argv distinction, path resolution, recursive directory behavior, operation distinctions, blanket network matching, scope boundaries, and deny-wins.
- Race duplicate answers and rule creation across workers.
- Verify automatic decisions are committed and published before provider resolution.
- Run the Grok interaction fixtures through both manual and rule-driven paths.
- Gate: every approval/question outcome is durable, owner-scoped, auditable, and delivered at most once.

## Phase 7 — Remaining adapters

Each subphase adds one adapter, its feature extra, strict schemas, manifest entries, fixtures, common contract-suite execution, and opt-in real-harness tests. Each is merged independently.

### Phase 7A — Cursor

- Reuse the ACP framing, routing, common events, permissions, and session machinery from Phase 4.
- Launch the tested Cursor `agent acp` interface used by the pinned T3 reference; reject versions where that command/schema is absent rather than falling back to print-mode JSON.
- Add Cursor-only session arguments, model/config options, mode mapping, permissions, question/plan extensions, native IDs, and resume cursors.
- Preserve Cursor’s tested steering semantics; otherwise advertise no steering and let orchestration queue.
- Gate: common adapter contracts, strict fixtures, create/resume matrix, and real-Cursor tests pass.

### Phase 7B — Codex

- Pin a tested `openai-codex` SDK release in the `codex` extra and record both SDK and SDK-managed runtime versions.
- Create one `AsyncCodex` context per active conversation even though the SDK supports concurrent turns.
- Use async `thread_start`/`thread_resume`, `AsyncThread.turn`, `AsyncTurnHandle.stream`, `steer`, and `interrupt`; these capabilities are present in the current official SDK. [OpenAI Codex Python SDK reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md)
- Revalidate SDK notification payloads through adapter-owned strict schemas before normalization.
- Map models, supported modes, approvals/sandbox settings, plans, reasoning, messages, tools/files, usage, terminal status, and errors.
- Reuse existing Codex authentication only. Do not expose SDK login/logout, install/update, binary discovery, thread fork, or arbitrary SDK options.
- Gate: isolated concurrent conversations, no-final-response completion, resume, interaction, and strict notification fixtures pass.

### Phase 7C — Claude Code

- Pin the official `claude-agent-sdk` in the `claude` extra and use one `ClaudeSDKClient` per conversation.
- Use the existing authenticated Claude state, `cwd`, explicit `cli_path` where configured, resume identifier, streaming receive loop, interrupt, and `can_use_tool` permission callback.
- Do not add package-owned authentication or binary lifecycle logic. The upstream SDK may supply its own bundled CLI dependency; that remains SDK-managed. [Claude Agent SDK for Python](https://github.com/anthropics/claude-agent-sdk-python)
- Normalize partial/final messages, reasoning, tool calls/results, plans, usage/cost, result status, and subagent activity.
- Advertise steering only if the pinned SDK/version has a tested active-turn steering primitive; otherwise queue.
- Gate: common contracts, bundled/system executable paths, resume, permissions, subagents, and real-Claude tests pass.

### Phase 7D — OpenCode

- Add `httpx` under the `opencode` extra.
- Supervise one `opencode serve` process per active conversation, bind it to `127.0.0.1` on a private allocated port, and avoid CORS or remote exposure.
- Use `/global/health` for startup/version probing, session/message APIs for commands, permission endpoints for approvals, and `/event` for the native SSE stream. [OpenCode server reference](https://dev.opencode.ai/docs/server/)
- Implement a strict, minimal SSE decoder and Pydantic models for the tested OpenAPI schema; do not generate or ship a general OpenCode client.
- Normalize session, message parts, reasoning, tools, files, plans, permissions, usage, aborts, and errors.
- Gate: common contracts, process death, HTTP/SSE reconnect, create/resume, permissions, and real-OpenCode tests pass.

## Phase 8 — Switching, projections, search, and retention

### Implementation

- Implement switching as a candidate-runtime transaction:

  1. Require the active turn to finish.
  2. Materialize the retained canonical handoff in order.
  3. Start a transient candidate native session and submit/seed the transcript.
  4. Persist the new binding and `harness_switched` event only after acceptance.
  5. Close the old native session after commit.
  6. On failure, close the candidate and persist only `harness_switch_failed`.

- Always create a new native session when changing harness kind, including switching back.
- Include retained user/assistant messages and canonical tool results in handoff. Exclude raw events, native prose summaries, and generated tool summaries.
- Centralize title projection with precedence: native, preserved manual, first eight words of earliest retained user message, then `Untitled conversation`.
- Replace the portable search query with:

  - PostgreSQL full-text vectors and GIN indexes.
  - SQLite FTS5 virtual tables.
  - One shared sanitized search-document builder so indexed knowledge is not duplicated.

- Search retained user/assistant content plus normalized canonical tool fields/tails. Exclude deleted/expired content, reasoning, raw native events, secrets, and full raw output.
- Implement archive, pin, snooze, soft-delete, and lightweight list projections.
- Add one externally scheduled command, `talktoharnesses_cleanup`, using a six-calendar-month UTC cutoff:

  - Delete complete expired turn aggregates.
  - Skip running/background-active conversations.
  - Cancel and delete expired waiting turns.
  - Recompute derived titles.
  - Rotate the native session after pruning.
  - Mark `requires_session_recreation` if rotation fails.
  - Permanently purge soft-deleted aggregates after the same interval.
  - Never touch workspace files.

### Tests and gate

- Test successful and failed switches, switch-back behavior, transcript rejection, and DB failure after candidate creation.
- Run search parity tests against PostgreSQL and SQLite.
- Test title precedence and title recomputation after retention.
- Test all retention states, aggregate deletion completeness, session rotation failure, and soft-delete purge.
- Gate: switching cannot damage the current binding, search results match across databases, and deleted context cannot remain in a resumable session.

## Phase 9 — Recovery, observability, and hardening

### Implementation

- On worker startup, acquire expired ownership and eagerly inspect running, waiting, and background-active conversations. Leave idle conversations for lazy resume.
- Recover commands from their durable delivery phase:

  - `accepted` but never `delivery_started`: safe to claim and deliver.
  - `delivery_started` without durable acknowledgement: mark `outcome_unknown`; never resend.
  - `delivered`: resume observation/session where the adapter supports it.
  - Settled commands are never redelivered.

- If a live stdio connection belonged to a failed worker, start a new runtime and use native resume when supported; do not claim zero-loss transfer.
- Implement executable-change resume attempts, transcript-based new-session fallback, harness-change new-session rules, orphaned-message interruption, and persisted recovery results.
- Add OpenTelemetry spans, counters, gauges, and histograms with a fixed low-cardinality attribute allowlist. Never attach prompts, commands, tool contents, environment values, file contents, or secrets.
- Instrument queue, startup/resume, turn, tool, interaction, SSE reconnect, process exit, truncation, recovery, usage, and cost behavior.
- Readiness requires database access, active worker capability, and at least one recently successful configured-harness probe. Return only a boolean and generic reason.
- Complete ten-second graceful shutdown and forced cleanup behavior.
- Add fault-injection hooks at durable boundaries solely for tests.

### Tests and gate

- Kill workers before/after claim, delivery marker, external call, native acknowledgement, event commit, and publication.
- Verify no ambiguous operation is retried and every recovery path records enough diagnostics.
- Test multi-worker PostgreSQL failover and the documented single-supervisor SQLite profile.
- Scan retained stderr, events, API errors, and telemetry exports for fixture secrets.
- Run the OS/database compatibility matrix.
- Gate: recovery is deterministic from durable state, no secret reaches an observable sink, and readiness/shutdown meet their contracts.

## Phase 10 — Release readiness

### Implementation

- Finalize machine-readable compatibility entries for all five adapters, including exact tested create/resume combinations.
- Generate and check in `SUPPORTED_HARNESSES.md`; CI fails if regeneration changes the file.
- Document installation extras, Django setup, ASGI lifespan integration, JWT issuance, owner scoping, PostgreSQL/SQLite operation, retention scheduling, recovery limitations, and opt-in real-harness tests.
- Document forward migration and upgrade procedures without promising backward migration compatibility.
- Audit public exports and remove implementation-only symbols from package `__all__` declarations.
- Enforce over 90% aggregate coverage in the dedicated coverage job; keep OS/provider/database jobs focused on their behavior.
- Add locked build and publish workflows using `uv build` and `uv publish`, distribution-content tests, and a release-tag check.
- Change `2026.8.0.devN` to `2026.8.0` only after every milestone gate and configured real-harness suite passes.

### Final acceptance

- Install the built wheel with `all`, and separately test minimal core and Django-only installs.
- Run Ruff, format check, strict Pyright, unit/property/contract/transcript/integration suites, PostgreSQL/SQLite suites, and manually configured real-harness tests.
- Verify all five adapters appear identically in the JSON and generated Markdown compatibility manifests.
- Execute the definition-of-done journey through both the Python facade and authenticated HTTP/SSE API.

## Phase 11 — Search, retention, and transcript product extensions

Owns product topics explicitly excluded from Phases 8–10 (recovery and release readiness do not cover them):

- **Search product:** ranking, snippets/highlighting, fuzzy search, stemming / per-language indexes, prefix/autocomplete, query operators, saved searches, or an external search service.
- **Retention product:** user-configurable retention periods, per-conversation exemptions, dry-run/report APIs, in-process scheduler, workspace cleanup, provider-side session deletion, or approval-audit deletion.
- **Transcript / handoff product:** public handoff or transcript import/export, exposing native session IDs, same-provider thread fork/clone, provider-native history import, LLM-generated summaries, handoff attachments, or reasoning/raw-event transfer.
- **Surface / adapter product:** a second conversation-list projection, new metadata endpoints, adapter-specific binding tables, generic runtime strategies, dynamic plugins, public `HarnessAdapter` protocol changes, or additional provider capabilities beyond the Phase 7 five-adapter set.

## Assumptions and locked defaults

- “Separately implementable” means sequential, independently reviewable merges, not independently deployable products or published releases.
- Phase 7A–7D each leave the full existing suite green.
- Exact harness and SDK version numbers are selected from the released versions actually exercised when an adapter phase begins; support is recorded as explicit versions, never inferred ranges.
- The host Django application owns Uvicorn invocation and may override binding; package examples and defaults use `127.0.0.1`.
- Retention means six calendar months in UTC, not a fixed number of days.
- Provider SDK-managed binary dependencies do not constitute package-owned installation/update behavior.
- No compatibility fallback, unsafe override, external broker, or dynamic adapter discovery is introduced.
