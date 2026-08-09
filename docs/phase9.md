# Phase 9 — Recovery, Observability, and Hardening

## Summary

- Merge the completed Phase 8 branch first, then implement Phase 9 as version
  `2026.8.0.dev9`.
- Add database-backed worker ownership and fencing so PostgreSQL workers can take over expired
  active conversations without allowing the previous worker to commit afterward. Enforce the
  documented single-supervisor profile for SQLite.
- Recover strictly from durable command delivery state. Only work proven not to have crossed the
  delivery boundary may be delivered. Ambiguous work becomes `outcome_unknown`; delivered work is
  observed through native resume where supported and is never submitted again.
- Eagerly inspect running, waiting, and background-active conversations at worker startup. Preserve
  Phase 4's lazy resume for idle conversations.
- Reuse Phase 8's canonical handoff and candidate-runtime path for a fresh native session when a
  native resume is unsupported or rejected. Do not create another transcript format or recovery
  runtime manager.
- Add OpenTelemetry traces and metrics through the library API only. The host owns SDK, processor,
  reader, exporter, and collector configuration.
- Replace component-revealing readiness output with a boolean/generic reason, complete the shared
  ten-second shutdown contract, and add fixed internal crash checkpoints solely for tests.
- Do not add command replay controls, public recovery/audit APIs, telemetry exporters, dashboards,
  configurable recovery policies, or Phase 10 release work.

## Current Baseline and Entry Conditions

Phase 9 starts only after the Phase 8 gate passes on SQLite and PostgreSQL. Reconcile the merged
checkout before implementation:

- `Command` already has `accepted`, `claimed`, `delivery_started`, `delivered`, `settled`,
  `coalesced`, and `outcome_unknown` states plus timestamps, owner, lease, attempt, native-session,
  and a free-text `recovery_result`. `DjangoPersistence.claim_commands()` currently reclaims only
  expired `claimed` rows; it does not classify `delivery_started` or `delivered` rows. Phase 9 must
  remove the free-text diagnostic rather than let exception text become a second recovery record.
- `CommandProcessor` persists `delivery_started` immediately before most adapter calls and
  `delivered` after the call returns, but it does not maintain a command lease for the lifetime of
  an observed turn. Harness switching must be brought under the same marker discipline before
  Phase 9 crash testing; no second delivery-state representation is added.
- Per-conversation serialization is process-local. There is no database conversation owner or
  fencing token, so two PostgreSQL workers can currently own different commands for one
  conversation or a stale worker can commit after its command lease expires.
- `TalkToHarnessesService.start()` reconciles interactions and starts the command claim loop. It
  does not scan active conversations, resume observation, or expose whether its background tasks
  are healthy.
- `RuntimeManager` can start, resume, reap, close, interrupt, shut down, reject stale Phase 8
  bindings, and create/seed/promote transient candidates. Keep these lifecycle paths and extend
  them with worker fencing and recovery reasons rather than adding a recovery-specific manager.
- Phase 8 supplies the one retained canonical handoff reader/renderer and session-rotation
  transaction. Recovery fallback must use them, reset native dedupe state for a fresh session, and
  never read raw/native events, reasoning, full tool output, stderr, or workspace files.
- `ProcessRecord` has no state for a process incarnation whose owning worker disappeared. A stored
  PID is diagnostic data and is not a safe handle: it may have exited or been reused.
- Assistant messages expose `interrupted`, but the aggregate does not currently retain whether an
  assistant-message completion event was seen. Add only the completion fact needed to mark a
  partial recovered message accurately.
- `/ready` currently checks database/service/broker state and returns component booleans. Phase 9
  replaces that response contract and adds recovery, worker, and recent-probe checks. `/health`
  remains process liveness only.
- The package has no direct OpenTelemetry dependency. Follow OpenTelemetry's library guidance:
  depend on `opentelemetry-api`, acquire global tracers/meters, and leave SDK/exporter setup to the
  host ([OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)).

Fix missing Phase 8 prerequisites on their owning branch before beginning Phase 9. Do not hide a
missing switch marker, incomplete session rotation, projection inconsistency, or adapter resume
contract behind recovery heuristics.

## Recovery Invariants

These rules are the source of truth for the implementation and its fault-injection assertions:

1. The relational database and canonical event log are authoritative. A provider session, child
   process, in-memory task, notification, or telemetry item never proves a durable transition.
2. At most one unexpired fenced worker owns a conversation. Every worker-originated command,
   lifecycle, interaction, and event commit verifies the same `(worker_id, fence)` in its database
   transaction.
3. A stale worker may close resources, but cannot change durable conversation state after its fence
   is replaced.
4. `accepted` or expired `claimed` with no delivery marker is proven undelivered. It may be claimed
   and delivered.
5. `delivery_started` without `delivered` is ambiguous even when a test knows the process died
   before the adapter call. Durable state alone cannot prove that fact, so the command is never
   sent again.
6. `delivered` means observe; it never means submit again. Native replay is deduplicated using the
   existing native ID/stream-offset state before new canonical events commit.
7. A settled, coalesced, or outcome-unknown command is terminal and is never reclaimed.
8. A new stdio/SDK/HTTP runtime after takeover is a new local incarnation. Native resume may restore
   provider observation, but Phase 9 does not claim zero-loss transfer of uncommitted bytes or
   events.
9. A transcript fallback creates a new native session from the retained canonical handoff. It
   cannot turn an ambiguous command into a retry; the ambiguous turn is terminalized first.
10. Recovery decisions and errors use fixed codes. Prompts, tool contents, provider text, paths,
    environment values, stderr, file contents, exception messages, and secrets are never copied
    into recovery diagnostics or telemetry.

## Public Contracts

### Python and adapter surfaces

Keep `HarnessAdapter`, `HarnessSession`, all provider request/event schemas, and the facade's
conversation and command methods unchanged. Recovery calls the existing `probe()`, `start()`,
`resume()`, `events()`, `interrupt()`, and `close()` methods.

`RuntimeManager` remains the public lifecycle implementation. Its worker-driven start/resume/event
commit paths receive an internal immutable fence returned by persistence; candidate sessions used
by the external cleanup command remain transient and do not acquire conversation ownership.

Do not expose worker IDs, fence numbers, recovery records, process IDs, native session IDs, probe
failure details, or telemetry configuration through the facade.

### Readiness HTTP contract

Keep `GET /api/v1/health` unchanged. Replace the `/ready` response with one shape for both outcomes:

```python
class ReadinessProjection(BaseModel):
    ready: bool
    reason: Literal["ready", "not_ready"]
```

- Return 200 with `{"ready": true, "reason": "ready"}` only when the database is usable, the
  process owns a live worker lease, claim/heartbeat tasks are healthy, the initial recovery scan is
  complete, and at least one configured harness has a successful probe inside the fixed freshness
  window.
- Return 503 with `{"ready": false, "reason": "not_ready"}` for every failure. Do not reveal which
  database, worker, broker, harness, executable, version, or probe failed.
- Readiness is a cached/query-only check. The request handler never probes a harness, takes over a
  conversation, starts a runtime, or performs recovery.

The OpenAPI route remains unauthenticated. No public recovery status or telemetry endpoint is
added.

## Work Package 1 — Worker Ownership, Fencing, and Migration

### Phase 9 migration

Create one migration after Phase 8 with the minimum durable additions:

- Add private conversation-owner columns to `ConversationAggregate`:
  `runtime_worker_id` (nullable), `runtime_lease_expires_at` (nullable), and
  `runtime_fence` (non-negative bigint, default zero). Index active status plus lease expiry for the
  startup scan. Do not copy these fields into aggregate JSON or public projections.
- Add a private `WorkerLeaseRecord` containing a lease slot, current worker ID, start/heartbeat
  times, expiry, and draining flag. PostgreSQL uses one slot per worker. SQLite uses the single
  fixed `sqlite-supervisor` slot, so a second live supervisor fails startup rather than operating in
  an unsupported multi-worker profile.
- Add a private append-only `RecoveryAttemptRecord` with recovery UUID, conversation FK, binding
  UUID, optional command/turn UUID, worker ID, fence, trigger, observed delivery phase, action,
  result, fixed reason code, and start/completion times. It contains no arbitrary message or JSON
  details and cascades only when its conversation is permanently purged.
- Replace `Command.recovery_result` with nullable `recovery_attempt_id`. The referenced
  `RecoveryAttemptRecord` is the one source for recovery outcome and reason. During migration,
  convert each existing non-empty free-text value to a fixed `legacy_unknown` attempt and discard
  the old text; never copy an arbitrary historical exception message into the new row.
- Add `completed` to the internal `Message` state and `MessageRecord`. Backfill it from existing
  `assistant_message_completed` events. User/system messages do not use this flag. Normal message
  materialization then writes it from the same started/completed transitions.
- Add `orphaned` to `ProcessStatus` and `orphaned_at` to `ProcessRecord`/`RuntimeProcess`. A takeover
  marks prior `starting` or `running` incarnations orphaned; it does not write a fabricated exit
  code or claim that the OS process exited.

The migration initializes ownership as unclaimed. It validates aggregate/event rows needed for the
message-completion backfill and fails on malformed data instead of treating every message as
partial. Reverse migration removes only Phase 9 private rows/columns and the internal completion
fact; it does not rewrite canonical history.

### Lease operations

Add coarse persistence operations rather than generic worker CRUD:

- Acquire/renew/mark-draining/release the process worker lease. Use database time for all expiry
  comparisons so worker clock skew cannot create two owners.
- Claim accepted commands only after atomically acquiring or renewing the command's conversation
  owner. Return an internal `ClaimedCommand(command, fence)` value.
- Claim expired active conversations in bounded batches for recovery. PostgreSQL uses
  `SELECT ... FOR UPDATE SKIP LOCKED`; SQLite serializes through its one supervisor lease.
- Renew all conversation leases owned by the worker in one bounded update and return the leases
  that were lost. A lost fence immediately cancels that process's pump/delivery task and closes its
  local runtime.
- Release an idle/reaped conversation lease. Do not release a lease while an active turn,
  interaction, background activity, delivery, candidate, or event batch remains live.
- Start a recovery-attempt row in the same transaction that takes over an expired conversation.
  When a later worker sees an unfinished attempt behind an expired fence, it closes it as
  `abandoned` with a fixed `worker_lost` reason before starting the next attempt.

Every persistence method called by `CommandProcessor`, `InteractionBroker` on behalf of a worker,
or a managed runtime must accept the fence and reject a mismatched/expired owner with a dedicated
internal stale-owner error. Owner-scoped facade mutations remain unfenced and continue to use
optimistic aggregate versions. Do not weaken owner scoping or turn a fence mismatch into an
optimistic retry.

Use one lease duration and renewal interval from the existing runtime policy. The persistence layer
receives database timestamps/expiry values from its own transaction rather than retaining the
current hard-coded 30-second claim duration in multiple implementations.

## Work Package 2 — Deterministic Startup and Command Recovery

### Startup coordinator

Add one application-layer `WorkerCoordinator` owned by `TalkToHarnessesService`. It is the sole
owner of the worker lease, conversation fences, heartbeat task, initial recovery pass, readiness
worker state, and draining transition. `CommandProcessor` consumes its fenced claims; it does not
start a second heartbeat loop.

Startup order is:

1. Start the committed-event broker.
2. Acquire the process worker lease. Refuse startup when the SQLite singleton lease is still live.
3. Mark readiness false and start the heartbeat before doing provider work.
4. Run the existing interaction publication/policy reconciliation. This republishes committed
   state only; it does not answer an ambiguous provider request again.
5. Claim and inspect running, waiting, and background-active conversations in capacity-bounded
   batches. Also inspect any expired command owner regardless of the aggregate's denormalized
   status. Do not start idle sessions.
6. Finish the initial scan, start normal fenced command claims, then allow readiness once the probe
   condition also passes.

An exception recovering one conversation records a fixed failure result, closes only that runtime,
and allows the scan to continue. Failure of the worker lease or heartbeat is process-wide: stop
claims, mark not ready, and close all runtimes because future commits are fenced out.

### Durable command decision table

Implement the table once in `application/recovery.py` as a pure classifier. The coordinator and
tests use the same result; adapters and repositories do not reimplement it.

| Durable state | Recovery action |
| --- | --- |
| `accepted` | Leave claimable; normal claim/delivery handles it. |
| Expired `claimed`, no `delivery_started_at` | Reclaim with a new fence and deliver once. |
| `claimed` with a delivery marker, or `delivery_started` | Persist command `outcome_unknown`; never call the adapter. |
| `delivered` with active/waiting/background work and resumable native state | Start a new local runtime, native-resume, import dedupe state, and observe events. |
| `delivered` without resumable native state | Terminalize unobservable work as outcome unknown/failed, then attempt canonical-handoff fallback. |
| `delivered` with no related live work | Record an invariant failure and settle it as `outcome_unknown`; do not invent success. |
| `settled`, `coalesced`, `outcome_unknown` | Record `no_action` when inspected; never claim or deliver. |

Apply command-kind effects in one recovery transition:

- An ambiguous submit, interrupt, steer, or answer-interaction terminalizes the active/waiting turn
  as `outcome_unknown`, cancels still-open interactions through the existing atomic broker path,
  and marks only incomplete assistant messages for that turn `interrupted=True`.
- An ambiguous switch leaves the current binding/runtime authoritative, marks the switch command
  `outcome_unknown`, and emits the existing `harness_switch_failed` event with a fixed recovery
  code. Candidate/native sessions that were not committed are not adopted.
- A delivered root command remains delivered while observation is successfully resumed. Its
  authoritative terminal provider event performs the existing atomic turn/command settlement.
- When observation cannot be restored, mark all still-running background activities failed with a
  fixed `worker_lost` summary before recomputing conversation status.

Persist the aggregate changes, command rows, canonical terminal/interaction/activity events, and
completed recovery-attempt row in one transaction. Publish only returned committed events. A crash
after this commit but before publication is repaired by ordinary SSE replay, never by re-running
the recovery transition.

### Continuous takeover

Startup is not the only failure window. The normal claim loop also scans for expired active owners
at the heartbeat cadence, subject to runtime capacity, so a surviving PostgreSQL worker can take
over after another worker dies. Reuse the startup classifier and recovery path. Do not poll on
SQLite beyond renewing the singleton supervisor.

## Work Package 3 — Runtime Reattachment and Session Fallback

### Orphaned local incarnations

On takeover, atomically mark the failed owner's `starting` and `running` process records
`orphaned`, set `orphaned_at`, and record the old binding/process relationship. Never attach to its
stdio, trust its PID, send a signal to a stored PID, or report an exit code. The new owner creates a
fresh adapter/runtime/process and uses only the persisted native session identity.

The old OS process may already be dead, may be terminated by its container/job owner, or may still
exist. Phase 9 guarantees fencing and no durable commits from it, not remote process adoption or
PID-based cleanup.

### Resume decision

Refactor launch preparation just enough to probe and build a prospective `LaunchSnapshot` before
mutating the active binding. Compare it with the binding's persisted launch snapshot:

- If the binding kind is unchanged, the fresh probe advertises resume, and a native session ID
  exists, attempt native resume even when the resolved executable, harness version, or adapter
  version changed. Record `unchanged_launch` or `executable_changed` as a fixed recovery reason.
- A successful resume commits the new process/launch history, session-resumed event, binding launch
  snapshot, and recovery result under the fence before the runtime is installed and its event pump
  begins.
- An unsupported/rejected/missing native resume is not retried in a loop. Terminalize any ambiguous
  live work first, clear the unusable native ID, and enter the fallback below.
- A binding/harness-kind mismatch is an invariant failure and never attempts cross-harness native
  resume. Legitimate harness changes continue to use Phase 8's switch command, fresh binding, and
  candidate transaction.

Provider error text is reduced to stable codes such as `resume_unsupported`, `resume_rejected`,
`provider_incompatible`, `executable_changed`, or `worker_lost`. The raw exception is neither
persisted nor attached to telemetry.

### Canonical-handoff fallback

Reuse `read_retained_handoff()`, `render_handoff()`, `RuntimeManager.start_candidate()`, and
`seed_candidate()` for the unchanged active binding:

1. In a fenced transaction, terminalize unobservable active work, rotate/clear the old native
   session with reason `recovery_fallback`, reset native ID/offset dedupe state, and mark the
   recovery attempt `fallback_started`.
2. Read the retained canonical handoff after that commit. The uncertain turn may appear only as
   canonical interrupted/outcome-unknown history; it is not resubmitted as a command.
3. Start and seed one transient candidate using the active binding ID. The current live-runtime map
   remains empty until the database accepts the replacement native session.
4. Commit the candidate native ID, launch snapshot, cleared recreation flag, session-rotated event,
   and recovery success under the same fence, then promote it when continued background
   observation is required. For an idle recovered conversation, close the candidate while retaining
   its durable resume identity, matching Phase 8 retention rotation.
5. On a normal candidate rejection, close it, set `requires_session_recreation`, and persist a
   fixed failure result. If the worker disappears after candidate delivery but before commit, the
   next worker marks that attempt abandoned and does not adopt or automatically reseed the unknown
   candidate.

Do not add provider-native transcript import, a public `seed_transcript()` method, provider-specific
fallback stores, arbitrary retry counts, or a path that submits the original command again.

## Work Package 4 — OpenTelemetry and Observable-Sink Hardening

### Dependency and composition boundary

Add `opentelemetry-api>=1,<2` as the package's instrumentation dependency. Add the matching
`opentelemetry-sdk` only to the development group for in-memory exporter tests. OpenTelemetry
traces and metrics are stable in Python, while exporters are separate packages
([OpenTelemetry Python status and packages](https://opentelemetry.io/docs/languages/python/)).

Create one small `application/observability.py` module that owns the instrumentation scope name,
constant span/metric names, fixed attributes, instruments, duration conversion, and committed-event
observation. Callers report typed enums/counts/timestamps; they cannot attach arbitrary dictionaries
or strings. With no host SDK configured, the standard API remains a no-op.

Do not configure a tracer/meter provider, exporter, endpoint, sampling rule, resource, logging
handler, or environment variables in the package. Do not add Django/HTTPX auto-instrumentation in
this phase.

### Fixed telemetry vocabulary

Use constant span names for worker recovery, command delivery, runtime start/resume, harness probe,
turn observation, interaction resolution, SSE replay/reconnect setup, and shutdown. A recovered
turn starts a new observation span; Phase 9 does not persist trace context or promise one span
across a crash.

Provide only these metric families:

- Counters: commands by kind/outcome, recovery attempts by trigger/action/outcome, process exits,
  stderr truncations, tool terminal outcomes, interaction requests/resolutions, SSE reconnects,
  and committed usage/cost observations.
- Observable gauges backed by the coordinator's last database sample: accepted queue depth, owned
  conversations, active runtimes, active turns, waiting interactions, and worker-ready state. Gauge
  callbacks never perform async database I/O.
- Histograms: command queue delay, runtime start/resume duration, startup recovery duration, turn
  duration, interaction wait duration, shutdown duration, and reported token/cost values. Derive
  durations from committed timestamps where possible.

Instrument canonical tool/interaction/usage/cost behavior only after the containing event commit.
Do not count an adapter event that lost an optimistic/fence race. Do not re-count historical events
during startup or SSE replay.

The complete attribute allowlist is finite and centralized:

- `tth.harness.kind`
- `tth.command.kind`
- `tth.operation`
- `tth.outcome`
- `tth.error.code`
- `tth.recovery.trigger`
- `tth.recovery.action`
- `tth.process.status`
- `tth.interaction.kind`
- `tth.tool.outcome`
- `tth.transport`
- `tth.database.system`

Each value must come from an enum or fixed internal mapping. Tool names, model/mode values,
executable/version strings, HTTP query values, IDs, worker names, and owner names are excluded even
when they appear harmless. This keeps metric dimensions low-cardinality; OpenTelemetry explicitly
treats high-cardinality metric attributes as opt-in concerns
([attribute requirement levels](https://opentelemetry.io/docs/specs/semconv/general/attribute-requirement-level/)).

Never use automatic exception recording because exception messages may contain provider or user
content. Mark spans with a fixed error code/status only. Never add span events containing payloads.

### Secret-safe observable sinks

Audit all paths that can retain or return provider-controlled text:

- Keep the existing streaming redactor as the one stderr/persistence-boundary redaction
  implementation. Do not add an observability-only redactor.
- Replace provider exception interpolation and traceback logging with fixed operation/error codes.
  Internal programmer failures may retain a traceback only when their type and message cannot
  contain provider/request data; default to the fixed-code path.
- Map provider/protocol/runtime domain failures to a centralized safe HTTP message table. Preserve
  stable error codes and the existing generic authentication/not-found behavior, but do not echo a
  raw `DomainError.message` merely because its code is known.
- Ensure recovery events/records, session/process failure events, provider warnings,
  incompatibility events, and turn failures contain an allowlisted code and generic message when
  their source was provider-controlled.
- Change ASGI lifespan failure messages and readiness failures to generic text. The underlying
  error is available only as a fixed diagnostic code/metric.

Prompts and canonical message history remain intentionally visible through owner-scoped
conversation APIs; this hardening applies to diagnostic/error/telemetry sinks, not to the product's
requested transcript surface.

## Work Package 5 — Readiness and Graceful Shutdown

### Recent harness probe

Add one process-local readiness monitor using a fixed five-minute freshness window and the injected
clock. It uses an internal persistence query for configured harnesses and the existing strict
adapter `probe()` plus `save_harness_probe()` path:

- On startup after the recovery scan, probe configured harnesses in deterministic order until one
  succeeds. A success refreshes the existing persisted probe projection and the cached readiness
  deadline.
- Refresh before the successful probe becomes stale. Probe only one known-successful harness per
  cycle; if it fails, try the remaining configured harnesses until one succeeds or the cycle ends.
- An empty registry/configuration set or all failed/stale probes leaves readiness false but does not
  stop the API, command recovery, or `/health`.
- Manual `POST /harnesses/{id}/probe` updates the same persisted source and notifies the monitor; it
  does not create a parallel readiness cache format.

The monitor reports only fixed failure codes to logs/telemetry and shuts down with the service. The
five-minute value is not a new public setting in Phase 9.

### One ten-second shutdown budget

Pass one monotonic deadline from `TalkToHarnessesService.stop()` through the coordinator,
`CommandProcessor`, `RuntimeManager`, readiness monitor, and publisher. Individual components do
not each receive a fresh ten seconds.

Shutdown order is:

1. Mark the worker draining and readiness false; stop new conversation/command claims.
2. Release claimed commands that have no delivery marker back to `accepted` under their fence.
3. Flush delta batchers and committed publication work, then interrupt active adapter operations
   concurrently through the existing interrupt deadline.
4. Settle confirmed terminal events. Before the shared deadline, persist `outcome_unknown` for
   locally owned operations whose delivery/terminal result remains ambiguous and mark partial
   assistant messages interrupted.
5. Gracefully close adapters, processes, candidates, broker/readiness/heartbeat tasks, and release
   idle conversation/worker leases.
6. Reserve the existing termination interval for force-killing process groups/Windows jobs and
   cancelling remaining owned tasks. If durable cleanup cannot finish, leave the lease to expire;
   do not release ownership early and permit a concurrent takeover while local tasks can commit.

Repeated shutdown calls remain idempotent. A forced path may leave an unfinished recovery attempt,
which the next fenced owner records as abandoned. The ASGI lifespan reports shutdown completion
within the shared budget after force cleanup; it never returns exception text to the server.

## Work Package 6 — Test-Only Fault Injection

Add one internal optional async checkpoint callback with a closed `FaultPoint` enum. It is injected
through application composition in tests and is `None` in production. Do not read an environment
variable, add a Django setting, expose an endpoint, or ship a general chaos framework.

The fixed checkpoints are immediately after these boundaries:

- command/conversation claim commit;
- delivery-started commit;
- adapter request write/receipt and native acknowledgement in fake adapters;
- delivered-marker commit;
- canonical event batch commit;
- committed-event publication;
- native resume commit; and
- fallback candidate seed and session-rotation commit.

Unit tests use barriers at the same points. Crash tests run a real worker subprocess, wait until the
selected checkpoint is reported over a test-only pipe, then terminate the worker externally. The
checkpoint callback never simulates a crash by catching an ordinary application exception, because
that would exercise cleanup paths that an abrupt worker loss does not run.

## Tests and Phase Gate

### Domain and persistence tests

- Table-test the pure recovery classifier across every command status, timestamp inconsistency,
  command kind, active/waiting/background state, resume capability, native ID, and recreation flag.
- Verify recovery terminalization cancels interactions, fails running activities, marks only
  incomplete assistant messages interrupted, preserves completed messages, and emits one ordered
  terminal event.
- Run worker/conversation lease contracts against SQLite and PostgreSQL: acquisition, renewal,
  expiry, fence increment, stale-commit rejection, draining, release, abandoned attempt closure,
  and database-time behavior under skewed application clocks.
- Race two PostgreSQL workers over the same and different conversations. Assert `SKIP LOCKED`
  distribution, one owner, monotonically increasing fences, one command delivery, and rejection of
  the losing worker's command/event/lifecycle commits.
- Start two SQLite services against one database and prove the second fails while the first lease is
  live, then succeeds after expiry. Keep SQLite tests single-supervisor after that assertion.
- Test Phase 9 migration/backfill/reverse behavior for message completion, owner fields, process
  orphaning support, and recovery rows. Run migration drift checks.

### Recovery and runtime tests

- Eagerly recover running, waiting, and background-active conversations; prove idle conversations
  create no adapter/runtime until the next command.
- Recover a delivered turn by native resume for every adapter fixture that advertises resume.
  Import persisted native IDs/offsets before reading replay and assert no duplicate canonical rows.
- Mark the old local process incarnation orphaned and start a distinct one. Assert no stored PID is
  signalled and no claim of zero-loss stdio transfer appears in events or diagnostics.
- Cover unchanged executable, changed resolved path, changed harness version, changed adapter
  version, rejected resume, missing native ID, unsupported resume, recreation flag, and
  harness-kind mismatch.
- Verify canonical fallback uses the exact Phase 8 handoff renderer, terminalizes ambiguous work
  before seeding, resets native dedupe identity, commits before promotion, closes rejected
  candidates, and never resubmits the original command.
- Kill during fallback seed and after rotation commit. The next worker must mark the first attempt
  abandoned or observe the committed rotation; it must not adopt an uncommitted session.
- Exercise startup capacity, recovery failure isolation, lease loss during event delivery, and
  shutdown during start/resume/candidate operations without leaking tasks or owned process trees.

### Crash matrix

For submit, answer-interaction, interrupt, steer, and switch where applicable, terminate workers at
each boundary and assert the durable result:

| Crash point | Expected recovery |
| --- | --- |
| Before claim commit | Command remains `accepted`; deliver once. |
| After claim, before delivery marker | Expired claim is safe; deliver once. |
| After delivery marker, before adapter call | `outcome_unknown`; no delivery, because persistence cannot prove that. |
| After provider receipt, before native acknowledgement | `outcome_unknown`; never resend. |
| After native acknowledgement, before `delivered` commit | `outcome_unknown`; never resend. |
| After `delivered` commit, before event commit | Native-resume observation; never resend. |
| After event commit, before publication | Replay the committed sequence over SSE; no duplicate event or provider call. |
| After publication, before terminal commit | Resume observation/dedupe; no duplicate committed sequence. |
| After terminal commit | Settled state is final; recovery performs no action. |

Run the subprocess matrix on PostgreSQL with a second worker taking over. Run the deterministic
single-worker subset on SQLite. Include a stale original worker that resumes after its lease is
stolen and prove every fenced write fails.

### Observability, readiness, and shutdown tests

- Use the in-memory OpenTelemetry SDK exporters to assert exact span/metric names, units, duration
  boundaries, committed-event timing, and the complete attribute allowlist. With no SDK provider,
  run the same application path and assert behavior is unchanged.
- Put unique fixture secrets in prompts, command argv, tool arguments/output, environment values,
  file content/paths, stderr chunks, provider errors, HTTP errors, and recovery failures. Scan
  retained stderr, canonical diagnostic events, recovery rows, API error bodies, captured logs, and
  serialized telemetry exports. Product transcript endpoints may contain their owner-scoped prompt;
  diagnostic sinks must not.
- Test `/ready` for database failure, worker lease loss, dead heartbeat/claim task, recovery in
  progress, no configured harness, stale probe, failed refresh, one fresh success, draining, and
  shutdown. Assert the body never changes from the two generic projections.
- Test the readiness monitor with an injected clock and fake adapters; prove route requests perform
  no probe and a manual successful probe refreshes the same state.
- Measure shutdown against hung claim, delivery, event pump, adapter interrupt/close, candidate,
  publisher, and process-tree fixtures. Assert the one shared budget, reserved forced cleanup,
  correct command recovery states, expired/unreleased leases on incomplete cleanup, and no live
  owned tasks/process descendants afterward.

### End-to-end and release gate

1. Start worker A on PostgreSQL, submit a turn, kill A at each crash checkpoint, start worker B,
   reconnect SSE, and prove the decision table, fencing, event order, and at-most-once delivery.
2. Recover a waiting interaction and a background-active conversation through native resume, then
   repeat with resume rejection and prove outcome-unknown/activity-failure plus canonical fallback.
3. Replace the configured executable between worker incarnations. Prove native resume is attempted
   once, successful resume preserves the binding, and rejected resume creates a fresh seeded native
   session without replaying the command.
4. Run the secret corpus through all five fake adapters and every observable sink, then run the
   telemetry cardinality assertion over the full suite.
5. Run Linux, macOS, and Windows process-tree/shutdown jobs plus SQLite/PostgreSQL suites, Ruff,
   format check, strict Pyright, migration drift, lockfile check, wheel/sdist builds, isolated core
   imports, and all prior phase gates.
6. Change the package and compatibility-document adapter versions to `2026.8.0.dev9` only after the
   complete gate passes and regenerate `SUPPORTED_HARNESSES.md` from its existing source.

The Phase 9 gate passes when recovery is a deterministic function of durable state, no stale worker
can commit, no ambiguous operation is resent, no secret reaches a diagnostic/telemetry sink,
readiness reflects real worker/probe capability without disclosing internals, and all owned runtime
resources stop within the shared ten-second budget.

## Implementation Order

1. Reconcile the completed Phase 8 branch, especially switch delivery markers, session rotation,
   candidate cleanup, binding checks, and all five resume fixtures.
2. Add the worker/conversation lease, recovery attempt, message completion, and orphaned-process
   migration plus SQLite/PostgreSQL persistence contracts.
3. Add fenced claim/commit operations and the worker coordinator; enforce SQLite singleton startup
   before implementing provider recovery.
4. Implement the pure command classifier and atomic recovery transition, then run the crash matrix
   through delivery/event publication.
5. Extend runtime launch preparation, native observation resume, executable-change handling, and
   Phase 8 canonical-handoff fallback.
6. Add centralized OpenTelemetry instruments and harden diagnostic events, logs, API errors, and
   lifespan messages against provider-controlled text.
7. Add the recent-probe readiness monitor and thread one ten-second deadline through shutdown.
8. Add subprocess fault checkpoints, multi-worker/fencing tests, secret scans, OS/database gates,
   and only then update the development version/generated compatibility document.

## Explicitly Out of Scope

- Manual retry/replay of `outcome_unknown` commands, a recovery admin API, public recovery records,
  or an operator command that overrides the decision table.
- Transfer or adoption of a failed worker's live stdio connection, SDK object, HTTP stream, process
  handle, PID, or uncommitted provider events; zero-loss takeover is not promised.
- Provider-side deletion of abandoned native sessions or PID-based cleanup of processes no longer
  owned by this supervisor.
- Persisted/distributed trace context, prompt tracing, dynamic telemetry attributes, automatic
  instrumentation, logs export, bundled SDK/exporters/collector, dashboards, alerts, or telemetry
  configuration APIs.
- Configurable lease, recovery retry, probe freshness, or fault-injection policies. Add settings
  only after a concrete deployment requirement.
- Phase 10 compatibility-matrix finalization, deployment/upgrade guide, performance targets,
  coverage gate, public export audit, publishing workflows, and stable release version.
- Phase 11 search, retention, transcript, projection, plugin, or provider-capability extensions.

## Assumptions

- PostgreSQL database time is the lease clock and all workers can reach the same durable database.
  Worker IDs are diagnostic ownership labels, while the monotonically increasing fence is the
  authority.
- SQLite is deployed with exactly one live supervisor. Its expired singleton lease permits crash
  restart; it is not a multi-worker queue.
- Adapter submission methods return only after the native request has been written/acknowledged as
  defined by the existing adapter contracts. A crash before the durable `delivered` marker remains
  ambiguous regardless of provider behavior.
- Native resume capability comes from the fresh strict probe and current binding, not from a guess
  based on harness kind. A resume may replay events and must pass through existing deduplication.
- All workers that may take over a conversation run with equivalent provider authentication and
  workspace access. Recovery validates paths again and does not weaken Phase 3 ownership checks.
- Phase 8's retained handoff is already canonical, redacted at its persistence boundaries, and
  excludes deleted context. Recovery does not introduce a second secret detector or transcript
  source.
