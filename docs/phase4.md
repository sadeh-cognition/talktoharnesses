# Phase 4 — Grok Vertical Slice and ACP Foundation

## Summary

- Merge the completed Phase 2 and Phase 3 branches first, then implement Phase 4 as version
  `2026.8.0.dev4`.
- Deliver the first end-to-end provider path: durable commands drive one isolated Grok process,
  ACP messages become canonical events, and a persisted native session can be loaded after idle
  reaping or a clean service restart.
- Add only the ACP v1 and Grok extension messages exercised by the pinned compatibility fixtures.
  The ACP package is shared implementation for Phase 7A Cursor, not a general ACP SDK.
- Run Grok as `grok agent --no-leader stdio`, with the configured model flag before `stdio` when
  present. Never pass `--always-approve`; permission requests must reach the package.

## Verified Protocol Baseline

- ACP's current stable wire protocol is version 1. Wire compatibility is negotiated through
  `initialize.protocolVersion`, independently of the version of a schema artifact or SDK
  ([ACP versioning](https://github.com/agentclientprotocol/agent-client-protocol#versioning)).
- ACP v1 uses newline-delimited JSON-RPC over stdio. A prompt stays pending while
  `session/update` notifications stream and completes with a `stopReason`; cancellation is the
  `session/cancel` notification
  ([ACP prompt lifecycle](https://agentclientprotocol.com/protocol/v1/prompt-turn)).
- Grok documents the long-lived `initialize` -> `session/new` or `session/load` ->
  `session/prompt` lifecycle, streamed message/thought/tool/plan updates, permission requests, and
  separate `x.ai/*` extensions
  ([Grok agent mode](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md)).
- A handshake-only probe on 2026-08-08 against local `grok 1.0.0` negotiated ACP v1 and advertised
  load, list, resume, and close session capabilities. It also emitted Grok control-plane
  notifications (`_x.ai/mcp/servers_updated`, `_x.ai/settings/update`, and
  `_x.ai/announcements/update`) immediately after initialization. This is discovery evidence, not
  a support claim; the compatibility entry is published only after create and resume fixture/live
  gates pass.

## Public and Internal Contracts

- Keep `HarnessAdapter` unchanged. `GrokAdapter` implements the existing probe, start, resume,
  submit, steer, interrupt, answer-interaction, events, and close methods; provider types never
  cross that boundary.
- Export `GrokAdapter` from `talktoharnesses.providers.grok`. Register its factory through Phase
  3's fixed `AdapterRegistry`; do not add entry-point discovery or a second registry.
- Keep `talktoharnesses.providers.acp` internal to provider implementations. Its usable surface is
  one connection class plus the allowlisted ACP models; do not re-export it from the top-level
  package as a supported generic client.
- Add a provider-neutral asynchronous command processor under `talktoharnesses.application`. It
  depends only on `Persistence`, `CommittedEventPublisher`, `RuntimeManager`, and the existing
  domain transitions. It is explicitly started and stopped by its owner; Phase 4 must not start
  workers from Django `AppConfig.ready()`.
- Reuse Phase 2's command, raw-event/native-deduplication, delta-batching, redaction, and event-batch
  operations. Add a coarse persistence operation only if the merged Phase 2 contract cannot
  atomically commit a command-state change with its projection and canonical events; do not expose
  ORM CRUD to fill that gap.
- Add only canonical payloads proven missing by the Grok fixtures. The known required addition is
  a native conversation-title update so `Conversation.title_native` changes in the same commit as
  an observable event. Keep provider request IDs, offsets, and extension data in native/raw records,
  not in public event payloads.

## Work Package 1 — Strict Compatibility Source

- Add one packaged machine-readable compatibility document. Define each tested Grok release once,
  including full CLI version/build identity, ACP protocol version, adapter version, and platform;
  have separate create and resume matrices reference those release records.
- Generate the provisional Grok section of `SUPPORTED_HARNESSES.md` from that document. Both probe
  enforcement and Markdown generation read the same source so supported versions cannot drift.
- Start with Grok 1.0.0 as the implementation target, but add its matrix rows only for combinations
  that pass the opt-in real create/resume suite. The full `grok --version` output and the
  `initialize` agent version must match the selected record.
- A missing binary, malformed version, unknown build, protocol mismatch, missing required
  capability, or create-only release used for resume fails with the existing strict provider
  incompatibility path. Opting into a real test never turns these cases into skips.
- Add empty `grok` optional-dependency metadata and include it in `all`; the adapter supervises the
  external executable and does not need an ACP Python SDK.

## Work Package 2 — Minimal ACP/JSON-RPC Connection

Create `talktoharnesses.providers.acp` with the following narrowly scoped behavior:

- Read Phase 3's single-consumer raw stdout stream incrementally. Preserve partial bytes until a
  newline and decode every complete line as exactly one UTF-8 JSON object. A single read may
  contain part of a frame, one frame, or several frames; an empty line is a malformed frame.
- Strictly decode JSON-RPC 2.0 requests, notifications, success responses, and error responses with
  Pydantic (`extra="forbid"`). IDs may be strings or integers but not booleans. Reject malformed
  JSON, invalid UTF-8, duplicate live IDs, responses for unknown IDs, invalid envelopes, and batch
  arrays.
- Serialize writes under one lock and await the `ProcessHandle` stdin drain. Allocate monotonically
  increasing local request IDs and keep one pending future per outbound request. Expose delivery
  (frame flushed) separately from the final response so durable command state does not wait for a
  whole prompt turn.
- Run exactly one stdout router task. It correlates responses, dispatches allowlisted inbound
  requests, and sends allowlisted notifications to the adapter. Request handlers may answer later,
  which is required for permission and structured-question interactions.
- Support ACP `session/cancel` and cancellation of local pending waiters. A response-vs-cancel race
  has one winner; late duplicates are protocol violations rather than a second resolution.
- On EOF, process exit, or explicit close, stop accepting writes, cancel the router, and fail every
  pending request exactly once. Close is idempotent and leaves no owned tasks.
- Decode ACP base schemas separately from Grok extension schemas. Do not accept arbitrary `params`,
  `_meta`, unknown methods, or unknown `sessionUpdate` variants. Tested Grok control-plane
  notifications that do not affect the canonical transcript are still strictly decoded, retained
  as redacted native input, and deliberately ignored.
- A malformed frame produces `protocol_error`; a valid but non-allowlisted method, field, request,
  notification, or update produces `unsupported_native_event`. Either error closes only the
  affected runtime. If a prompt was already delivered, atomically mark its command/turn
  `outcome_unknown`; before delivery, record session failure without pretending the prompt ran.

The initial ACP schema allowlist is:

- `initialize` request/result and the capability fields consumed by this package.
- `session/new`, `session/load`, `session/prompt`, and `session/cancel`.
- `session/update` with only message chunk, thought chunk, tool call, tool call update, plan, and
  usage update variants present in pinned fixtures.
- `session/request_permission` and its option/outcome response.
- Session model/mode requests only when the pinned Grok release advertises them and their behavior
  is covered by fixtures.

The initial Grok extension allowlist includes the three post-initialize control notifications
observed above. Add interjection, structured-question, title, and nested-activity schemas only from
captured 1.0.0 fixture shapes; method names alone are not sufficient evidence of compatibility.

## Work Package 3 — Grok Adapter and Normalization

### Launch, probe, and sessions

- Construct argument tuples directly: `agent`, `--no-leader`, optional `--model <id>`, then
  `stdio`. Use Phase 3's resolved executable and process supervisor. Do not invoke a shell, mutate
  the inherited environment, auto-install/update Grok, or manage login/logout.
- Probe the version first, reject it against the compatibility source, then perform an ACP
  initialization handshake. Advertise only capabilities that both the adapter implements and the
  pinned agent reports. A Grok extension without a capability bit is available only when the exact
  compatibility entry and fixtures allowlist it. In particular, `supports_steer` is false unless
  the tested Grok interject extension is available.
- Advertise no client filesystem or terminal capability unless a fixture proves that the package
  implements the corresponding reverse requests. Grok should execute its own local tools under the
  supervised process instead of acquiring an unimplemented client surface.
- Start with `session/new`, the resolved primary working directory, and no package-injected MCP
  servers. Resume with a fresh process/connection and `session/load` using the retained native
  session ID. Apply a configured mode only through a tested advertised session-mode method and
  reject an unknown model or mode before accepting a turn.
- Treat history notifications emitted during `session/load` as native resynchronization, not new
  conversation content. Use Phase 2's native ID/offset deduplication so replay cannot append a
  second canonical message, tool, usage record, or activity.

### Turn control and interactions

- `submit` installs one active canonical turn context, sends one text `session/prompt` request, and
  returns after the frame is flushed. A private response watcher converts the eventual stop reason
  into the authoritative terminal harness event.
- Map `end_turn`, `max_tokens`, and `max_turn_requests` to completed turns with the native reason;
  map `cancelled` to interrupted; map `refusal` and JSON-RPC prompt errors to failed. Terminal
  settlement does not depend on receiving a final assistant-message chunk.
- Implement `steer` only with the tested Grok interject extension. A method-not-found or explicit
  unsupported result returns `False` so the existing steer-or-queue transition preserves the
  prompt. Other protocol errors fail the command; steering must never silently consume text.
- `interrupt` first resolves all pending permission/question requests as cancelled, sends
  `session/cancel`, and returns once written. The original prompt response remains the terminal
  authority; Phase 3's interrupt timeout handles a hung agent.
- For each reverse permission/question request, allocate a canonical interaction, emit it before
  waiting, and retain the JSON-RPC responder by interaction ID. `answer_interaction` maps the
  submitted canonical answer back to one advertised native option and responds once. Phase 4 does
  not add persistent approval rules; those remain Phase 6.

### Canonical mapping

Keep all Grok-specific interpretation in one stateful normalizer owned by one adapter instance:

| Native input | Canonical output |
| --- | --- |
| `agent_message_chunk` | message started/delta; message completed when its ID changes or the turn ends |
| `agent_thought_chunk` | reasoning started/delta; reasoning completed at its tested terminal boundary |
| `tool_call` / `tool_call_update` | tool requested/started/output/completed/failed plus command or file events when the typed tool data proves that subtype |
| `plan` | one stable plan per turn with created then updated events |
| `usage_update` | usage and cost fields with documented native meaning; do not invent token categories |
| `session/request_permission` | approval interaction requested, followed by one durable resolution |
| tested Grok question extension | structured-question interaction requested and resolved |
| tested Grok title/activity extensions | native title projection and nested activity start/completion |
| prompt response | authoritative turn terminal event and command settlement |

- Scope every notification to the adapter's active native session and turn. A mismatched session,
  update without an active turn, illegal lifecycle transition, conflicting reuse of a native
  ID/offset, or terminal response for the wrong request is a protocol error. A byte-equivalent
  duplicate is deduplicated without emitting a second canonical event.
- Derive stable canonical IDs from persisted native IDs where present. For messages without native
  IDs, persist and deduplicate the native stream offset; never generate fresh IDs when replaying a
  loaded session.
- Accumulate redacted full structured tool input/output in the existing retained tool projection.
  Construct the handoff tail through `CanonicalToolResult`, preserving its single UTF-8-safe 2 KiB
  limit as the source of truth. Do not implement a second truncation algorithm in the adapter.
- Preserve parent IDs for subagent/background activity. Turn completion and background completion
  are independent; live activity continues to suppress Phase 3 idle reaping after the parent turn
  ends.

## Work Package 4 — Durable Command and Event Pump

- Add one command-worker loop that claims up to the capacity granted by Phase 2, renews leases
  while delivery or a turn is live, and serializes work per conversation. The database claim and
  Phase 3 runtime limit remain the authorities; do not add an in-memory global queue.
- For executable submit, steer, interrupt, and answer-interaction commands:

  1. Re-read the owner-independent worker snapshot after claim and obtain or lazily resume the
     conversation runtime.
  2. Commit `delivery_started` before invoking the adapter.
  3. Invoke the adapter and commit `delivered` only after its native frame has been flushed.
  4. Consume normalized events through one pump per runtime. Feed deltas into Phase 2's 50 ms
     batcher; each flush atomically updates projections, native dedupe state, and canonical events.
  5. Publish only the events returned by the successful commit.
  6. On the authoritative prompt terminal event, atomically update the turn and settle its command,
     then publish that committed terminal batch.

- Route state changes through the existing pure transitions. Add a single event-to-transition
  dispatcher only where native payloads need projection changes; do not duplicate transition rules
  in the adapter, worker, and repository.
- A publisher failure never rolls back or repeats a committed event. Leave the event replayable and
  surface the publisher failure through worker diagnostics; the next state decision comes from
  persistence.
- Lazy resume is used both after idle reaping and after a clean service restart. The runtime manager
  creates a fresh adapter/process, validates the current executable again, loads the retained native
  ID, and installs the runtime only after the lifecycle and launch snapshot commit succeeds.
- Clean shutdown stops claims, lets the existing Phase 3 shutdown path interrupt active work, and
  settles confirmed terminal results. Any operation still ambiguous at the shutdown deadline is
  marked `outcome_unknown`, not retried.
- Startup reclaims only commands that Phase 2 proves were never delivered. A
  `delivery_started`/`delivered` command without a terminal result is not guessed or replayed in
  Phase 4; mark it `outcome_unknown`. Executable-change fallback, transcript seeding, and general
  ambiguous recovery remain Phase 9.

## Test Plan

### ACP unit tests

- Feed frames one byte at a time, split at every boundary, and coalesce multiple lines in one read.
  Cover invalid UTF-8/JSON, arrays, blank/invalid envelopes, unknown fields and methods, duplicate
  IDs, unknown response IDs, out-of-order responses, JSON-RPC errors, and EOF with pending requests.
- Exercise concurrent outbound requests, delayed reverse-request responses, response/cancel races,
  write serialization, close during write/read, repeated close, and task-leak checks.
- Assert stdout contains only protocol frames and stderr never reaches the decoder, using Phase 3's
  deterministic helper process.

### Adapter and fixture tests

- Add versioned Grok fixtures for initialize, create, load, prompt terminal reasons, message and
  reasoning streams, every supported tool/command/file shape, plans, usage/cost, title, permissions,
  structured questions, steering, cancellation, nested/background activity, provider errors, and
  the allowlisted no-op control notifications.
- Replay fixtures through the real connection and normalizer, not by calling mapping helpers with
  already-decoded models. Assert ordered canonical payloads and redaction of raw events, errors,
  tool inputs, and full outputs.
- Cover duplicate native IDs/offsets, history replay on load, permission-answer races,
  cancel-with-pending-permission, steer fallback without message loss, no-final-message completion,
  activity after turn completion, unknown update variants, malformed frames, and abnormal process
  exit.

### Persistence and lifecycle tests

- Run command-worker scenarios against both Phase 2 SQLite and PostgreSQL suites: claim renewal,
  delivery-state ordering, commit-before-publish, terminal settlement, publisher failure,
  concurrent commands for one conversation, and runtime capacity.
- Persist a conversation, reap it, submit again, and prove `session/load` continues without duplicate
  canonical history. Repeat after a clean worker/service restart with no in-memory runtime state.
- Crash test before `delivery_started`, between `delivery_started` and flushed delivery, after
  delivery, before terminal commit, and after terminal commit. Assert that only proven-undelivered
  work is retried and every ambiguous case becomes `outcome_unknown`.
- Verify an unsupported/malformed native input closes and fails only its conversation runtime while
  other conversations continue and no child process or adapter task leaks.

### Real Grok and release gate

- Add opt-in create and resume tests selected by an explicit environment flag and executable path.
  When enabled, missing authentication, a missing binary, or an unlisted/mismatched version fails.
- Capture sanitized transcripts from the pinned binary and review them before committing fixtures;
  never commit auth paths, tokens, machine names, or workspace-specific content.
- Gate with Ruff, format check, strict Pyright, full tests, lockfile check, migrations check, wheel
  and sdist builds, compatibility Markdown regeneration check, and imports proving core and ACP
  remain Django-free.
- Phase gate: a persisted Grok conversation can execute, stream, request permission, interrupt,
  survive idle reaping, and continue after a clean restart without event gaps or duplicate
  canonical content. Malformed or unsupported native input affects only that conversation.

## Explicitly Out of Scope

- The full ACP specification, ACP v2 draft, a public generic ACP client, an ACP SDK dependency, and
  arbitrary Grok `x.ai/*` methods.
- Cursor or any other concrete adapter; Cursor consumes the ACP foundation in Phase 7A.
- HTTP/Python facade lifecycle, JWT, SSE, and public interaction endpoints from Phase 5.
- Persistent approval rules and automatic decisions from Phase 6.
- Harness switching, transcript handoff, search, and retention work from Phase 8.
- Ambiguous crash recovery, executable-change fallback, observability, and readiness from Phase 9.
- Binary discovery, installation, updates, authentication management, arbitrary CLI arguments,
  environment overrides, package-supplied MCP servers, or dynamic provider plugins.

## Assumptions and Preconditions

- The current checkout contains Phase 1 plus planning work; it is not the implementation baseline.
  Phase 2 must supply durable commands/batching/redaction/native dedupe and Phase 3 must supply the
  factory registry, required launch snapshots, process supervisor, and runtime manager described in
  their accepted plans.
- Grok uses the current Django OS user's existing authenticated CLI state. Phase 4 does not weaken
  provider permissions or broaden filesystem access.
- The first vertical slice accepts text prompts. Other ACP content blocks are decoded only when
  required for provider output; image/audio/resource prompt submission is not added without a
  separate requirement.
- Additional workspace roots are not advertised to Grok unless the pinned release exposes a tested
  session field for them. The primary resolved working directory remains required.
