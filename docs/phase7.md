# Phase 7 — Remaining Adapters

## Summary

- Merge the completed Phase 6 branch first, then implement Phase 7 as version
  `2026.8.0.dev7` in four independently mergeable increments: Cursor, Codex, Claude Code, and
  OpenCode.
- Keep `HarnessAdapter`, `HarnessSession`, the command processor, `InteractionBroker`, canonical
  events, persistence operations, and the fixed `AdapterRegistry` unchanged as public contracts.
  Provider-native objects remain private to one adapter instance and one conversation runtime.
- Add strict compatibility data, native schemas, transcript fixtures, common contract tests, and
  opt-in live create/resume tests with each adapter. A release enters the published matrix only
  after its corresponding live gate passes.
- Reuse implementation only where the second concrete use proves it is shared: Cursor reuses the
  ACP transport, and Phase 7A extracts the compatibility envelope/rendering shared with Grok.
  Codex, Claude, and OpenCode retain separate normalizers because their native lifecycle semantics
  differ.
- Add no authentication flows, binary installer/updater, arbitrary provider options, generated
  OpenCode client, dynamic adapter discovery, provider-specific persistence, or new public API.

## Verified Baseline and Compatibility Risks

- The locally installed Cursor CLI `2026.08.04-aaa8809` exposes `agent acp` as a long-lived ACP
  server. This is discovery evidence only. Capture its initialization response and complete native
  transcripts before adding a compatibility row. Cursor's public CLI documentation covers version,
  models, and resume behavior but does not define the ACP wire extension surface
  ([Cursor CLI reference](https://docs.cursor.com/en/cli/reference/parameters)).
- The official Codex Python SDK exposes one `AsyncCodex` context, `thread_start`, `thread_resume`,
  `AsyncThread.turn`, and per-turn `stream`, `steer`, and `interrupt`. Its terminal result may have no
  final response, so turn completion must remain independent of assistant-message completion
  ([Codex Python SDK reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md)).
- The current Codex SDK's high-level async API does not document a public deferred approval handler.
  Its lower-level implementation has SDK-owned approval handling, including a default accept path.
  Phase 7B must not pin a release until the tested public async surface can expose, defer, and answer
  command and file approval requests. It must not reach through private `_client` attributes or let
  the SDK auto-accept requests
  ([Codex SDK client source](https://github.com/openai/codex/blob/main/sdk/python/src/openai_codex/client.py)).
- The official Claude Agent SDK bundles Claude Code by default, accepts an explicit `cli_path`, and
  provides `ClaudeSDKClient`, `cwd`, streaming responses, `interrupt`, resume options, and an async
  `can_use_tool` callback. The callback is the only Phase 7C permission path
  ([Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python),
  [Claude SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions)).
- OpenCode exposes a loopback `opencode serve` process, `/global/health`, session/message endpoints,
  a permission response endpoint, and `/event` SSE. The pinned executable's OpenAPI document and
  captured events, rather than the moving documentation page, define the adapter allowlist
  ([OpenCode server reference](https://dev.opencode.ai/docs/server/)).

The locally installed versions observed while writing this plan are Cursor
`2026.08.04-aaa8809`, Codex CLI `0.147.0`, Claude Code `2.1.226`, and OpenCode `1.2.27`.
They are probe candidates, not support claims or dependency pins.

## Phase Boundary and Shared Contracts

Phase 7 starts only after the Phase 6 gate passes. Before adding an adapter, verify these merged
contracts:

- `InteractionBroker` force-commits and publishes each interaction request before policy evaluation
  or provider resolution. An adapter only translates a native waiter into the existing
  provider-neutral request and later translates the durable `InteractionAnswer` back to that waiter.
- `CommandProcessor` owns delivery markers, 50 ms batching, committed publication, terminal command
  settlement, steer fallback, native deduplication, and event-pump failure behavior. An adapter must
  not write persistence or publish events directly.
- `RuntimeManager` owns one runtime per conversation, idle reaping, clean resume, shutdown, and
  launch history. An SDK that can multiplex threads still gets a fresh SDK client per conversation.
- `HarnessConfiguration` remains the only stored configuration shape. `model` and `mode` map to a
  finite tested provider choice; adapters must not add arbitrary SDK dictionaries, command flags,
  environment overrides, provider credentials, or MCP configuration.
- The Phase 1 transcript format already supports stdio, HTTP, and SSE records. Extend its enum only
  if a captured native transport cannot be represented; do not create one fixture format per
  provider.
- Canonical tool output continues to use `CanonicalToolResult` as the sole UTF-8-safe 2 KiB handoff
  truncation rule. Every adapter retains redacted full tool input/output through the existing
  projection and must not implement another tail algorithm.

No Phase 7 increment requires an ORM schema or migration. If a fixture proves that a canonical event
or provider-neutral interaction field is missing, add the narrow domain field and its existing
materialization path in that same subphase; do not store the native payload in a public projection.

## Shared Work Introduced When Needed

### Compatibility source and generated documentation — Phase 7A

The second compatibility document makes the release/matrix envelope and Markdown rendering shared
knowledge. In 7A:

- Extract the provider-neutral fields from Grok's compatibility models into one internal module:
  adapter version, release ID, platform list, provider runtime identity, capabilities, create
  matrix, and resume matrix. Keep ACP version, SDK version, CLI build, and provider methods in
  strict provider-specific release details.
- Keep one packaged JSON document per harness. Do not introduce database-backed compatibility,
  network lookup, version ranges, or a plugin schema.
- Replace the Grok-only renderer with one deterministic renderer that reads the installed provider
  documents in fixed `HarnessKind` order and generates all completed sections of
  `SUPPORTED_HARNESSES.md`. Probe enforcement and Markdown generation must read the same models.
- Preserve separate create and resume matrices. A release record is an implementation target; only
  a matrix row is a published support claim. Include the operating system and, for SDK adapters,
  the exact SDK and SDK-managed runtime versions in the row identity.
- Make regeneration a test and packaging gate in every subphase. Do not hand-edit provider tables.

### SDK-managed runtime seam — Phase 7B

Cursor and OpenCode remain ordinary `ProcessBoundAdapter` implementations. Codex and Claude are the
first real uses of SDK-managed subprocesses, so add the lifecycle seam only when 7B begins:

- Add one private, runtime-checkable SDK-managed marker protocol. Codex and Claude opt into it;
  absence of the marker preserves the current process-supervised path. Do not add a public runtime
  strategy registry or factory layer.
- Allow `ManagedRuntime.process` to be absent for marked adapters. The adapter's `close()` and
  `interrupt()` own the SDK client, while `RuntimeManager` still owns timeouts, one-runtime locking,
  idle reaping, persistence, and shutdown ordering.
- Persist the existing `ProcessRecord` as the runtime-incarnation record with `pid=None` for an
  opaque SDK-managed process. Retain `STARTING`, `RUNNING`, and terminal status changes so lifecycle
  recovery does not gain a second schema. Process stdout/stderr events exist only when a supervised
  `ProcessHandle` exists.
- Build the SDK launch snapshot by strictly resolving the working directory and workspace roots.
  Set `resolved_executable` only when the SDK exposes the exact selected executable path. An
  executable-scoped approval rule cannot match a launch whose SDK-managed executable is opaque.
- Continue applying the existing start/resume, interrupt, graceful close, idle reap, and total
  shutdown deadlines to both runtime styles. Add branch-specific cleanup, not duplicate managers.

### Registration and optional dependencies

- Update the existing default ASGI composition to register all completed adapter factories exactly
  once in the fixed registry. Do not add entry points or scan installed packages.
- Keep provider modules importable without their extras. Import Codex/Claude/httpx lazily at adapter
  construction or probe and raise the existing strict provider-incompatible error when a required
  dependency is absent. Core and Django-only installs must still import successfully.
- Add `cursor`, `codex`, `claude`, and `opencode` extras and include them in `all`. Cursor is empty;
  Codex and Claude pin the exact SDK release exercised by fixtures/live tests; OpenCode contains the
  tested compatible `httpx` range. The lockfile records exact development artifacts.

## Phase 7A — Cursor ACP Adapter

### Deliverables

- Add `talktoharnesses.providers.cursor` with `CursorAdapter`, launch arguments, strict probe and
  compatibility models, Cursor extension schemas, and one stateful normalizer.
- Add `cursor.json`, sanitized transcript fixtures, unit/contract tests, opt-in live tests, a fixed
  registry entry, the empty `cursor` extra, and the generated Cursor support section.
- Keep `talktoharnesses.providers.acp` internal. Cursor does not add an ACP SDK dependency or expose a
  generic ACP client.

### ACP reuse and strict schemas

- Parameterize `AcpConnection` with the adapter's outbound methods, inbound request handlers,
  notification decoders, and session-update decoder. This replaces the current Grok-global
  allowlist; it is not an open-ended registration API and remains internal to the two ACP adapters.
- Preserve the existing framing, JSON-RPC ID correlation, single stdout router, serialized writes,
  delivery marker, deferred reverse requests, cancellation, and close behavior.
- Split protocol-identical ACP v1 models from `grok_ext` and new `cursor_ext` models. Unknown fields,
  methods, session-update variants, response IDs, or extension payloads close only that runtime with
  `unsupported_native_event` or `protocol_error` as already defined.
- Extract only canonical normalization that is byte-for-byte identical for Grok and Cursor after
  fixtures prove it. Cursor-specific titles, questions, modes, resume cursors, tool metadata, and
  terminal reasons stay in `CursorNormalizer`.

### Probe, launch, and sessions

- Resolve the configured Cursor executable through the existing supervisor and run the exact
  version command. Match full version/build output and platform against `cursor.json`.
- Launch `agent acp` directly with no shell. Pass `--yolo` only when
  `HarnessConfiguration.yolo` is true. Do not use `--print`, stream JSON,
  login/logout, update, arbitrary flags, or environment mutation as fallbacks.
- Initialize ACP v1, validate the complete tested agent identity and capabilities, and advertise
  only the intersection implemented by the adapter and reported by the pinned CLI.
- Create with `session/new` and the resolved working directory. Resume with the exact tested
  `session/load`/Cursor cursor fields; treat load history as resynchronization and import the
  persisted native-ID/offset dedupe set before emitting live events.
- Obtain models and modes only through pinned ACP methods/initialization fields. Reject an unknown
  configured model or mode before accepting a turn. Additional workspace roots remain unadvertised
  unless a tested Cursor session field supports them.

### Turns, interactions, and normalization

- Send one text prompt and return after the ACP frame drain. A private prompt-response watcher emits
  the authoritative terminal event even when no final assistant message exists.
- Implement steering only when the pinned capability and fixture prove the active-turn method and
  response. An explicit unsupported result returns `False`; the existing command processor then
  queues the text. Other failures do not consume the steer command silently.
- On interrupt, cancel pending native interaction waiters through their tested native outcome, send
  `session/cancel`, and leave the prompt response as terminal authority.
- Convert each permission/question reverse request into the existing broker request, retaining only
  its opaque responder and correlation ID in memory. `answer_interaction` validates the selected
  option and writes exactly one JSON-RPC response.
- Map message and reasoning chunks, tools/results, file/command facts, plans, usage, title, questions,
  and nested activity only from captured schemas. Preserve native parent and item IDs and reject
  events for the wrong session or without an active turn.

### Tests and 7A gate

- Replay raw split/coalesced ACP frames through the real connection and normalizer for every
  allowlisted common and Cursor extension shape. Cover malformed frames, unknown fields/methods,
  duplicate IDs/offsets, load replay, terminal-without-message, and redaction.
- Run the shared adapter contract for start, resume, delivery, steer-or-queue, interrupt,
  multi-interaction, terminal settlement, close, and fresh-instance isolation.
- Run opt-in create and resume tests against an explicit Cursor executable. When enabled, missing
  auth, binary, version, method, or capability is a failure rather than a skip.
- Gate the independently mergeable increment on the full existing suite, Ruff, format, strict
  Pyright, lockfile/migration checks, builds, import isolation, and deterministic support-doc
  regeneration.

## Phase 7B — Codex SDK Adapter

### Entry condition and deliverables

- Select and pin an `openai-codex` release only after a spike proves its public async API supports
  all required operations: start/resume, turn stream, steer, interrupt, model discovery, strict
  runtime metadata, and a deferred approval/question request that can be answered after an
  indefinite await.
- If the public release still auto-resolves approvals or exposes the responder only through private
  attributes, leave the Codex create/resume matrices empty and do not merge 7B as complete. Do not
  substitute direct app-server JSON-RPC, the synchronous client, SDK source patches, or
  auto-approval; those would contradict the selected SDK contract and Phase 6.
- Add `talktoharnesses.providers.codex`, `codex.json`, strict adapter-owned notification schemas,
  normalizer, fixtures/tests, registry entry, pinned `codex` extra, and generated support section.

### SDK lifecycle and compatibility

- Create exactly one `AsyncCodex` context per conversation and keep one active `AsyncThread` and at
  most one active `AsyncTurnHandle`, even if the SDK can multiplex turns. Never share the context
  through a module singleton or registry factory.
- Reuse existing authenticated Codex state. Do not call login/logout, change accounts, discover or
  install binaries, request updates, fork/archive threads, or expose arbitrary `CodexConfig`.
- Record `openai_codex.__version__`, SDK initialize metadata, and the SDK-managed runtime package and
  runtime version in the compatibility record and launch snapshot. All values must match the
  selected release row before start/resume succeeds.
- Map finite canonical modes to tested `Sandbox` and approval-mode combinations. Never select a mode
  that hides approval requests required by the broker. Pass only resolved `cwd`, selected model,
  selected mode, and tested fixed settings to `thread_start`/`thread_resume`.
- Use the thread ID as `native_session_id`. Resume through `thread_resume`, import persisted native
  dedupe state, and do not emit SDK history/read results as new canonical transcript content.

### Turns, interactions, and normalization

- Start a turn with `AsyncThread.turn`, retain its handle, and consume `stream()` in one adapter-owned
  task. `submit` returns after the SDK confirms turn creation, which is the native delivery boundary.
- Route `steer` and `interrupt` only to the retained active handle. Return `False` only for the
  pinned SDK's explicit unsupported/not-active response; map other failures normally.
- The SDK approval callback validates its raw method/payload through strict adapter schemas, emits a
  broker interaction, and awaits the matching answer without blocking the SDK event reader. Map
  command argv and file path/operation only when typed native fields prove them. Never use display
  text or shell parsing to manufacture Phase 6 approval actions.
- Revalidate every upstream notification by serializing its public fields into adapter-owned frozen
  Pydantic models with `extra="forbid"`. SDK model acceptance alone is insufficient because an SDK
  update can broaden a union without updating this adapter.
- Normalize assistant deltas/items, reasoning, plans, command/file/tool lifecycle, usage/cost, and
  errors by native thread/turn/item IDs. Complete the canonical turn from the SDK terminal status;
  `final_response=None` must still settle it.

### Tests and 7B gate

- Use a fake public SDK surface, not private client classes, to test context cleanup, two isolated
  conversations, turn delivery, stream ordering, steer, interrupt, resume, and missing-final-response
  completion.
- Fixture every allowlisted notification and approval request before normalization. Reject an
  upstream object containing a new field or union member until its fixture and schema are reviewed.
- Exercise manual and persistent-rule approval paths through the real `InteractionBroker`, including
  duplicate answers, interrupt while waiting, and provider response after committed publication.
- Run opt-in live create/resume/interaction tests against the exact SDK/runtime pair. Then run all
  cross-provider and packaging gates from 7A, including minimal installs without the Codex extra.

## Phase 7C — Claude Code SDK Adapter

### Deliverables and SDK lifecycle

- Add `talktoharnesses.providers.claude`, `claude.json`, strict message/block schemas, normalizer,
  fixtures/tests, registry entry, pinned `claude` extra, and generated support section.
- Create one `ClaudeSDKClient` per conversation using `ClaudeAgentOptions`. Enter it during adapter
  start/resume and disconnect it during close; the Phase 7B SDK-managed runtime branch owns the
  surrounding deadlines and persistence.
- Use the SDK-bundled CLI by default. If `HarnessConfiguration.executable_path` is set, resolve it
  with the existing executable security checks and pass it as `cli_path`. Do not search `PATH`,
  install/update Claude Code, manage login, mutate the environment, or pass `extra_args`.
- Record the exact SDK version and the initialized Claude Code version for bundled and explicit-CLI
  matrix rows. A configured CLI must match its row; the bundled and system executable paths are
  distinct tested combinations.
- Pass the resolved `cwd`, selected model, tested `permission_mode`, resume ID, and `can_use_tool`
  callback only. Do not add package MCP servers, hooks, system prompts, agent definitions, or SDK
  plugins.

### Sessions, turns, and interactions

- For a new session, connect without a prompt and obtain the native session ID from the first strict
  initialization/result shape before publishing session start. For resume, set the exact saved ID
  and same resolved working directory; reject a mismatched returned session ID.
- `submit` sends one text query and returns after the SDK write completes. Run exactly one
  `receive_response()` consumer until its `ResultMessage`; a missing result is not a successful turn.
- Map `interrupt` to the SDK method. Advertise no steering unless the pinned public SDK adds and the
  live suite proves a distinct active-response steering primitive; ordinary `query()` during an
  active response is not treated as steer.
- Implement `can_use_tool` as an async deferred responder. Strictly normalize the tool name/input and
  permission context, submit it to the broker, await indefinitely, then return the tested
  `PermissionResultAllow` or `PermissionResultDeny`. Map cancel to the provider's tested denial/
  interrupt outcome and never pre-populate `allowed_tools` in a way that bypasses the callback.
- Treat `allow_session` as the tested provider-native session decision only when the callback result
  can express it. Package persistent rules continue to answer one request at a time as Phase 6
  requires.

### Normalization and tests

- Revalidate `SystemMessage`, `AssistantMessage`, `UserMessage`, and `ResultMessage`, plus every
  supported content block, through adapter-owned strict Pydantic schemas. Unknown message/block
  variants fail the runtime rather than being ignored.
- Normalize text, reasoning when present, tool use/result, typed command/file facts, result usage and
  cost, errors, and native session/model metadata. Map task/subagent messages with their native
  `agent_id`/parent tool-use ID into nested activities and allow those activities to outlive the
  parent turn only when fixtures prove that lifecycle.
- Use the `ResultMessage` status as terminal authority and complete any open message/reasoning/tool
  streams first. Do not infer success from the last assistant block.
- Test bundled and explicit CLI selection, missing extra, strict schemas, start/resume, same-cwd
  enforcement, no steering fallback, interrupt, permissions, subagents/background activity, usage/
  cost, errors, redaction, and cleanup without leaked SDK tasks.
- Run opt-in live create/resume/permission/subagent tests for each published SDK/CLI pair, followed by
  the full shared gate.

## Phase 7D — OpenCode HTTP/SSE Adapter

### Deliverables and private server

- Add `talktoharnesses.providers.opencode` with launch arguments, loopback HTTP client, minimal SSE
  decoder, strict OpenAPI-derived schemas, adapter/normalizer, compatibility data, fixtures/tests,
  registry entry, `httpx` extra, and generated support section.
- Keep OpenCode process-bound. Launch one `opencode serve` per conversation with the existing process
  supervisor, resolved working directory, `--hostname 127.0.0.1`, a private port, no mDNS, no CORS,
  and no shell.
- Allocate a loopback ephemeral port immediately before spawn. If the server loses the bind race,
  close that pre-session incarnation and retry once with a fresh port; never retry after command
  delivery. Persist every attempted process incarnation through the existing lifecycle operations;
  only the one that passes health may reach the session-start boundary.
- Build one `httpx.AsyncClient` per adapter with the loopback base URL and finite connect/write
  deadlines. Long-lived SSE reads have no provider-silence timeout; the runtime's existing warning
  policy remains diagnostic only.

### Probe, sessions, and HTTP delivery

- Match `opencode --version` to `opencode.json`, start the server, and require `/global/health` to
  return `healthy=true` with the same exact version before creating a session. Validate the pinned
  OpenAPI schema during fixture capture, not on every production startup.
- Create via the pinned `POST /session` request and use its ID as `native_session_id`. Resume by
  fetching the exact session ID from a fresh server rooted at the same working directory; a missing
  session is a resume failure and not an implicit new session.
- Deliver prompts through the pinned asynchronous message endpoint so `submit` returns on the HTTP
  acknowledgement rather than waiting for a completed assistant message. Include only text, selected
  model/agent mode, and a package-generated stable native message ID.
- Map interrupt to the pinned session abort endpoint. Advertise no steering unless a pinned endpoint
  explicitly mutates an active response; sending a second message is queueing, not steering.
- Reply to permissions through the pinned permission endpoint with `remember=false`; Phase 6 rules,
  not OpenCode server memory, remain the durable policy source.

### Strict SSE and normalization

- Implement only the SSE behavior exercised by fixtures: incremental UTF-8, LF/CRLF lines, comments,
  `event`, `id`, and one or more `data` lines joined by newline, with dispatch on a blank line.
  Reject malformed UTF-8, invalid fields in pinned events, invalid JSON, oversized retained partial
  frames, and unknown event union members. Do not add a general SSE package or generated client.
- Open `/event` before accepting the first prompt and require its tested connected event. Filter all
  native events by the primary session or its proven child-session relationship; a per-process server
  does not make unrelated events canonical.
- Normalize session status, message/part deltas, reasoning, tools/results, typed command/file changes,
  todos/plans, permission requests, usage/cost, child sessions, aborts, and errors through native
  session/message/part/call IDs.
- On an SSE disconnect while the process is alive, reconnect and resynchronize from session status
  and message endpoints, then suppress already persisted native IDs/part revisions. If the adapter
  cannot prove the terminal state of a delivered prompt, emit `outcome_unknown`; general crash
  recovery remains Phase 9.
- Treat terminal session status as turn authority, independent of a final text part. Process death
  continues through the existing lifecycle pump and affects only that conversation.

### Tests and 7D gate

- Test split SSE bytes/lines, multi-line data, comments, disconnect/reconnect, resync dedupe, unknown
  events, health/version mismatch, session-not-found resume, HTTP errors, permission races, abort,
  process death, bind retry before delivery, redaction, and client/process cleanup.
- Replay captured HTTP request/response/SSE sequences end to end through the adapter and common
  command processor. Do not unit-test only mapping helpers.
- Run opt-in real create/resume/permission/reconnect tests against an explicit OpenCode executable.
  Publish only rows whose exact version and platform pass.
- Gate on all earlier adapters and the full repository quality, packaging, compatibility-generation,
  import-isolation, SQLite, and PostgreSQL suites.

## Common Adapter Contract Suite

By the end of 7D, parameterize one provider-neutral suite over all five adapter factories. Each
provider supplies a fake native transport/SDK but the assertions remain shared:

- probe returns strict kind/version/capabilities and rejects unsupported versions;
- start/resume return an isolated session with a stable native ID and immutable launch snapshot;
- submit returns at the native delivery boundary and terminal status settles without requiring a
  final assistant message;
- steering either succeeds once or returns `False` without losing the prompt;
- interrupt and close are idempotent at the orchestration boundary and leave no owned tasks,
  processes, clients, callbacks, or pending responders;
- multiple interactions remain independent and every answer reaches the provider at most once after
  committed publication;
- native IDs/offsets survive reap/resume without duplicate canonical events;
- malformed or unsupported input fails only the affected runtime; and
- two conversations never share an adapter, SDK context, HTTP client, native waiter, or event queue.

Provider fixture suites still test native details. The common suite must not grow provider switches
or encode one provider's event names as the canonical contract.

## Merge and Release Gates

For each of 7A–7D:

1. Capture and sanitize probe, create, turn, interaction, terminal, interrupt, close, and resume
   transcripts for the selected exact release.
2. Implement strict schemas and fixture replay before enabling the live registry entry.
3. Run the common adapter, command processor, broker, runtime lifecycle, persistence, and API/SSE
   suites. No endpoint changes are expected because existing projections are provider-neutral.
4. Run Ruff, format check, strict Pyright, unit/property/contract/transcript/integration tests,
   SQLite/PostgreSQL jobs, migration drift, lockfile check, wheel/sdist builds, minimal-extra import
   tests, and support-document regeneration.
5. Run the opt-in real create and resume tests. Add only passing exact combinations to the matrix,
   regenerate `SUPPORTED_HARNESSES.md`, and leave the full existing suite green before merging.

The Phase 7 gate passes when Grok, Cursor, Codex, Claude Code, and OpenCode all execute through the
same durable command/interaction/event contracts, each advertised create/resume combination is
strictly pinned and live-tested, and a fault in one provider runtime cannot affect another
conversation.

## Explicitly Out of Scope

- Harness switching and transcript handoff, projection/search changes, and retention from Phase 8.
- Ambiguous restart recovery, executable-change fallback, observability, readiness, and generalized
  SSE/process recovery from Phase 9.
- Authentication setup, token/API-key storage, login/logout, browser/device flows, binary discovery,
  installation, updates, arbitrary CLI flags, environment mutation, or credential forwarding.
- ACP v2 or the complete ACP specification, a public ACP SDK, a generic HTTP/SSE client, generated
  OpenCode bindings, direct Codex app-server integration, or patched/private SDK APIs.
- Images, audio, resource prompt blocks, structured output schemas, thread/session fork/archive/
  delete/share, custom MCP servers, hooks, plugins, provider agents, slash commands, and shell
  endpoints unless a later requirement explicitly adds them.
- Dynamic adapter plugins, entry-point discovery, provider-specific database tables, a second event
  pump, a second interaction policy engine, or new public synchronous wrappers.

## Assumptions

- Subphases merge sequentially as 7A, 7B, 7C, and 7D. “Independently mergeable” means each leaves
  the repository releasable and green; it does not mean they are implemented against divergent base
  branches.
- Exact provider/SDK version pins are chosen from artifacts actually exercised when each subphase
  starts. The candidate versions recorded above are deliberately not written into dependency or
  support matrices by this planning change.
- SDK-managed binary dependencies are provider-owned runtime resources. This package still owns the
  conversation lifecycle, durable delivery contract, timeouts, and shutdown call into the SDK.
- Text prompts remain the only input form. Provider outputs may contain typed file/tool/reasoning
  structures when fixtures prove them.
- A provider capability absent from the pinned public surface is advertised as false. If it is
  required for the phase gate—especially observable approvals—the subphase waits for a compatible
  release rather than adding a fallback that weakens the contract.
