---
type: log
title: Wiki Log
status: maintained
audiences:
  - product
  - developer
tags:
  - type/log
last_verified: 2026-08-30
verified_against_commit: 78003994d9fe93108ce5a6bc3591ab2e2ef904d9
---

# Wiki Log

Entries are appended using `## [YYYY-MM-DD] operation | Title`.

## [2026-09-21] fix | Keep authentication state local to each request

- Replaced separate HTTP and SSE retry loops with one HTTPX authentication flow.
  SSE reconnections get fresh authentication retry state after transport failure.
- Documented the final-token rejection callback and its HTTP/SSE regression
  coverage, so credential stores handle rejection before caller error translation.

## [2026-09-20] implement | Resolve shared client credentials per request

- Added async token providers to the official HTTP client. A changed token
  permits one retry after a 401; HTTP bodies, idempotency keys, and SSE cursors
  survive that retry. Provider clients leave rotation and revocation to the
  credential store.
- Recorded real HTTP test evidence and updated the client interface and docs.

## [2026-09-18] operation | Harden workspace setup lifecycle

- Review follow-ups on workspace provisioning: setup moved out of the
  manager into `runtime/workspace_setup.py`, whose `prepare_workspace` now
  serves client starts, recovery resumes and candidate runtimes (recovery
  previously skipped setup, so an image or manifest change went unchecked).
- `WorkspaceSetupRecorder` owns the setup event commits and writes no
  process row, so a late `on_started` from the uncancellable docker exec
  thread can no longer revive a failed process; conflicts are retried instead
  of escaping as a retryable `optimistic_conflict`.
  `SandboxManager.prepare_workspace` also stops delivering the callback once
  it has returned. The sandbox raises a typed `WorkspaceSetupFailed`
  (`reason`, `exit_code`, `output_tail`) instead of a details dict the
  runtime had to pick apart.
- Runner: SIGKILL is sent to the whole process group after the grace period
  independent of the shell's fate; the `fcntl` import is deferred so the
  proxy package imports on Windows.
- Proxy: setup output is kept as a rolling 4 KiB tail and logged line by line
  while streaming instead of accumulating in memory.
- Updated [Provision sandbox workspaces](requirements/provision-sandbox-workspaces.md)
  (behavior, gap, acceptance criteria).

## [2026-09-18] ingest | Sandbox workspace provisioning

- Added the approved product source `raw/product/sandbox-workspace-provisioning.md`
  (approved 2026-09-18): TTH owns project-environment setup inside sandboxes
  through a repo-declared `.tth/setup.sh`, toolchains download on demand into
  `/data`, and the split service's runtime is invisible to agents.
- New requirement [Provision sandbox workspaces](requirements/provision-sandbox-workspaces.md)
  and decision [Sandbox toolchain hygiene](decisions/sandbox-toolchain-hygiene.md);
  updated split-services, isolated-harness-runtimes, deployment, the HTTP API
  map, requirements-by-status and the index.
- Implementation: root-owned `/opt/tth/venv` and Node/corepack in every
  rendered Dockerfile, managed toolchain env and `SandboxManager.prepare_workspace`,
  the in-container `workspace_runner`, `RuntimeManager` events
  `workspace_setup_started` / `workspace_setup_completed`, error code
  `workspace_setup_failed`, and split-side `private_env.seal`.
- Verified against `f7e2c5e25226f669f969fe6ff5fbf25b8af5fa96` plus the
  workspace provisioning work in the working tree; the live gate
  `tests/live/test_sandbox_workspace_live.py` passed against a codex image.

## [2026-09-02] operation | Document the runtime close endpoint

- Added `POST /conversations/{id}/runtime/close` to the HTTP API map and the
  HTTP and SSE interface page, with its 204 and 409 `conversation_busy`
  outcomes (turn, activity or switch in flight; runtime held by another
  worker).
- Verified against the working tree on top of
  `53bc585` (sandbox harnesses using Docker).

## [2026-08-30] ingest | On-demand sandbox provisioning

- Added the approved product source `raw/product/on-demand-sandbox-provisioning.md`
  (approved 2026-08-30), superseding the operator-configuration and fail-closed
  statements of `raw/product/split-service-runtime-ownership.md`.
- The proxy now spawns each kind's Docker sandbox on demand, builds a missing
  image locally, records sandboxes with their split tokens in the database,
  reattaches after restart, and returns `sandbox_preparing` /
  `sandbox_unavailable` errors; `TTH_SANDBOX_KINDS` and `TTH_SPLIT_<KIND>_URL`
  are removed, and readiness probing never spawns or builds.
- Updated probe-and-configure-harnesses, deployment, testing-guidelines,
  provider-adapters, system-context, runtime-isolation,
  isolated-harness-runtimes, split-services, compatibility-and-adapters,
  host-django journey/requirement, and target-users pages.
- Verified against baseline `2920f5820783245bd5b871e61edd44642ebfe56a` plus the
  on-demand sandbox work present in the working tree.

## [2026-08-30] implement | Add the local wiki lint command

- Copied Agentbahn's deterministic wiki metadata, link, title, requirement
  section, and orphan checks into `talktoharnesses.wiki_lint`.
- Added the `wiki_lint` Django management command using this project's existing
  `host/manage.py` entry point and documented the local invocation.
- Added focused lint-engine and command tests.
- Verified against TalkToHarnesses baseline
  `78003994d9fe93108ce5a6bc3591ab2e2ef904d9` plus the uncommitted command and
  documentation recorded with this entry.

## [2026-08-30] operation | Record Cursor ACP token-usage limitation

- Preserved Cursor's upstream ACP reports and local live verification as the
  engineering source
  `raw/engineering/cursor-acp-token-usage-limitation.md`.
- Corrected the cross-provider token-usage requirement from `implemented` to
  `partially-implemented` without changing its approved intent or acceptance
  criteria.
- Recorded that Cursor's adapter accepts native usage but verified Cursor Agent
  releases omit it from ACP, leaving the strict live gate failing rather than
  synthesizing token values.
- Verified against TalkToHarnesses baseline
  `78003994d9fe93108ce5a6bc3591ab2e2ef904d9` plus the uncommitted Cursor usage
  normalization and documentation corrections recorded with this entry.

## [2026-08-29] operation | Align documentation with split runtime ownership

- Preserved the pre-split product sources and added
  `raw/product/split-service-runtime-ownership.md` to record their approved
  supersession.
- Updated architecture, operations, requirements, capabilities, domain, maps,
  and journey pages to assign provider execution and executable discovery to
  split services.
- Recorded the workspace support-matrix aggregator and the live-gate requirement
  for a reachable split.
- Verified against split-services commit
  `47644027875773ba520cbfdd9f978d196a548802` plus the uncommitted documentation
  and release-check corrections recorded with this entry.

## [2026-08-29] operation | Consolidate split projects

- Moved all six per-kind split projects and the schemas-only `tth-types`
  project into the TalkToHarnesses repository as top-level directories.
- Preserved the independent package, HTTP API, lockfile, and Docker sandbox
  boundaries; only repository-relative paths and documentation changed.

## [2026-08-29] implement | Split services: tth-proxy, tth-types, per-kind splits

- Split the monolith at the HarnessAdapter seam: the package becomes tth-proxy
  (with one generic remote adapter); each kind moved to a tth-<kind> project as
  a Django+Ninja HTTP+SSE split service reachable directly or in a Docker
  sandbox; shared wire schemas moved to the schemas-only tth-types package.
- Updated architecture pages (system context, runtime isolation, provider
  adapters, layered architecture, technology stack) and added
  [Split services decision](decisions/split-services.md).

## [2026-08-21] implement | Report harness token usage

- Preserved the approved cross-harness token-usage requirement and the updated
  live-testing procedure as immutable raw sources.
- Normalized provider-reported token usage for all six harnesses without
  inventing omitted categories or backfilling historical turns.
- Made meaningful pre-terminal usage mandatory for every provider's successful
  live create and resume turns.
- Verified against TalkToHarnesses baseline
  `c996cbcd23b7cbf4f6b4d70422ab17ce715661bf` plus the uncommitted
  implementation recorded with this entry.

## [2026-08-21] implement | Issue client JWTs from Django admin

- Preserved the approved Django admin client-token request under
  `raw/product/django-admin-client-token-issuance.md`.
- Added trusted admin issuance for active Django users while retaining the
  existing one-active-token rule and avoiding a remote issuance endpoint.
- Updated the JWT requirement, Django capability, host journey, deployment
  guidance, and README onboarding.
- Verified against TalkToHarnesses baseline
  `7cb2e2c82909ebe01fc3eb68220d7764adab64bd` plus the uncommitted
  implementation recorded with this entry.

## [2026-08-21] implement | Locate process-bound CLIs from kind

- Removed `executable_path` from harness create/configuration contracts.
  Grok, Cursor, OpenCode, and Prime Agent binaries are located at probe and
  launch from PATH or `TALKTOHARNESSES_*_EXECUTABLE`. Codex and Claude stay
  SDK-bundled. New and stored configuration containing `executable_path` is
  rejected and must be recreated.
- Preserved the approved TTH-owned executable-discovery source, which
  supersedes the older statement that TTH never discovers external CLIs.
- Updated README create examples and derived probe, harness-instance, glossary,
  adapter, overview, and upgrading pages.
- Verified against TalkToHarnesses baseline
  `3f90f85a37028a1ba0498cff641ef5c8a1bec6d7` plus the uncommitted
  implementation recorded with this entry.

## [2026-08-21] operation | Record TTH abbreviation

- Added product source `raw/product/abbreviation.md`.
- Documented TTH as the abbreviation for TalkToHarnesses in the
  [glossary](glossary.md), [overview](overview.md),
  [product overview](maps/product-overview.md), and vault home.

## [2026-08-20] operation | Propose orchestration interaction test harness

- Added engineering source `raw/engineering/orchestration-interaction-test-harness.md`.
- Added proposed analysis [Orchestration Interaction Test Harness](analyses/orchestration-interaction-test-harness.md).
- Recorded a test-evidence gap on approval requirements and testing guidelines.

## [2026-08-20] operation | Remove product-name mentions from the vault

- Replaced named-product examples with generic remote HTTP clients, wiki web
  viewers, and the `wiki_lint` command.

## [2026-08-20] operation | Move lint handoff rule to development guidelines

- Moved the `make lint` handoff sentence from repository `AGENTS.md` into
  [Development guidelines](operations/development-guidelines.md).
- `AGENTS.md` now routes agents to the wiki instead of restating that rule.

## [2026-08-20] operation | Create TalkToHarnesses LLM wiki

- Added an Obsidian vault at `llm-wiki/`.
- Snapshotted README, accepted ADRs, and operator docs under `raw/`.
- Authored maps, capabilities, requirements, journeys, architecture, interfaces, domain, decisions, and operations pages for the public contract.
- Verified against TalkToHarnesses baseline
  `bb3d2b755500fc663816d6cbd1a7cd7947a8920b` plus uncommitted floor-and-probe
  compatibility work present in the working tree.

## [2026-09-05] implementation | Muse Code split integration

- Added Muse Code to adapter, compatibility, deployment, and requirement evidence.
- Inspected baseline `92bdf81138628204f7b58df5f1f80545abdbbde3` plus the Muse Code working-tree changes.
- Muse uses the official MSP v1 interface in a self-contained split; provider
  execution remains behind the existing proxy HTTP/SSE contract.
- Protocol tests use Meta's published SDK conformance transcripts. Live
  create/resume usage passes; the full gate remains blocked by native Muse
  approval-ledger durability errors. Direct CLI steering and interruption pass.
- Core non-live tests pass; coverage remains 90.14%, identical to the untouched
  baseline and below the existing 91% gate. The repository-wide format check
  also reports pre-existing files outside the Muse changes.

## [2026-09-05] implementation | Align Muse approval handling with the SDK

- Inspected baseline `92bdf81138628204f7b58df5f1f80545abdbbde3` plus working-tree Muse changes.
- Matched SDK approval notification/receipt handling, per-connection monotonic
  UUIDv7 command IDs, acknowledgment identity checks, and bounded retries for
  explicit overload/backpressure errors with unchanged command identities.
- Concurrent and replayed answers share the first delivery outcome, including
  native failures. Protocol tests cover these behaviors and structured questions.
- Tightened the live gate to reject persisted answer commands with unknown
  outcomes: interaction counts and completed turns had hidden native failures.
- Reproduced the same ledger error with Meta's SDK after resuming a completed
  session outside both TTH and Docker. Fresh-session SDK approvals succeeded
  on the host and in Docker. The native failure after resume remains unresolved.
- Muse tests and lint pass; the stricter live gate fails on approval delivery.

## [2026-09-06] implementation | Attach streamable HTTP MCP servers to harness configuration

- Preserved the approved MCP servers requirement under `raw/product/`.
- Added `HarnessMcpServer` and `HarnessConfiguration.mcp_servers` to
  `tth-types`, the `supports_mcp_servers` capability flag, shared
  provider mappings in `tth_types.mcp`, and the API request body.
- Claude Code, Cursor, Grok, and Codex adapters pass configured servers to
  the SDK, ACP, and config overrides; OpenCode, Muse Code, and Prime Agent
  reject them with `provider_incompatible`.
- The proxy rewrites loopback server URLs to the sandbox host gateway for
  managed sandboxes. Regenerated `SUPPORTED_HARNESSES.md` with the new column.
- Updated the probe-and-configure requirement, harness instance, unified
  adapters, glossary, and README.

## [2026-09-06] implementation | Attach MCP servers to Muse Code hosts through a private settings directory

- Preserved the Muse Code amendment to the MCP servers requirement under
  `raw/product/`.
- tth-muse renders a per-host `XDG_CONFIG_HOME` with the harness's servers
  merged into `settings.json` and links to `auth.json` and `trust.json`,
  passes it through a new `ProcessSpec.environment`, and removes it on close.
- Muse advertises `supports_mcp_servers`; the rejection was removed and
  `SUPPORTED_HARNESSES.md` regenerated. Updated the requirement, capability
  page, and README.

## [2026-09-06] implementation | Refactoring pass: split drift guard and proxy write path

- Re-synced the vendored split modules (supervisor `env` support, SSE callback
  typing, per-kind executable tables, ACP outbound allowlist, runtime
  re-exports, model discovery stderr fallback) and added
  `scripts/check_split_drift.py` to the static CI gate. Removed the unused ACP
  JSON-RPC package from `tth-prime-agent` and unused `sse_decoder` copies.
- Proxy: projections are now incremental per commit and the search document
  rebuilds only for text-bearing batches; one worker snapshot per delta batch;
  lock-free owner-scoped snapshot reads; single serialization on SSE replay;
  one shared commit body; `@_db_thread` replaces the hand-written
  `sync_to_async` wrappers; `remote/sandbox.py` split into orchestration,
  `docker_ops`, and `sandbox_auth`.
- Updated the split-services decision, development guidelines, and
  `docs/refactoring-scan.md`.

## [2026-09-10] implementation | Report turn usage through one shared accumulator

- Six adapters each hand-rolled the accumulation the canonical `usage_updated`
  payload is defined to carry, with three int-validation rules and four terminal
  policies between them. `tth_types.usage.TurnUsage` now holds the one rule set
  and each adapter keeps only its wire-shape mapping.
- Codex reads the turn's totals as the difference between two of the thread's
  running totals instead of summing the per-request figures, and the terminal
  usage branch no production notification could reach was removed with the
  schema field behind it.
- Grok no longer derives a total its live frames never sent, and a response
  frame trailing the turn's terminal figures can no longer restart the running
  total or raise outside a turn.
- Token volume is recorded once per turn, when its terminal event arrives, into
  its own instrument; `tth.token_cost` carries cost alone.
- Recorded in [Report Harness Token Usage](requirements/report-harness-token-usage.md)
  and [Conversation Event](domain/conversation-event.md).

## [2026-09-09] implementation | Preserve active ACP turns after unmatched replies

- Updated the synchronized Grok/Cursor connection copies to discard replies
  without live waiters while continuing exact-ID response correlation.
- Added regressions for unmatched success/error responses, string versus integer
  IDs, duplicate replies, cancellation, and Grok usage/terminal delivery.
- Recorded the behavior and upstream uncertainty in [Provider Adapters](architecture/provider-adapters.md).

## [2026-09-09] implementation | Publish the binding's harness and approval policy on the conversation

- `ConversationDetail` gained `harness_id` and `yolo`, filled from the active
  binding beside the `harness_kind`, `model`, `mode`, and `effort` it already
  carried.
- A conversation outlives the harness it was opened on, so a client resuming one
  previously had no way to learn which harness ran it, and had to supply an
  approval policy of its own rather than the one the conversation was created
  with. Both facts live on the binding; the detail is where they are published.
- Both `ConversationDetail` builders (`django/persistence.py` and the in-memory
  test persistence) were updated. Nothing else changes: routes return the domain
  model directly, so the fields reach the HTTP response and the official client
  without a schema change.
- Recorded in [Create and Manage Conversations](requirements/create-and-manage-conversations.md)
  and [Conversation](domain/conversation.md).

## [2026-09-18] implementation | Broker Codex MCP tool approvals

Codex's `mcpServer/elicitation/request` tool confirmations now use canonical
approval interactions with the MCP server and tool identity preserved. The
adapter waits for a decision and sends the MCP action/content response.
Tests cover approval, denial, cancellation, interruption, and separation from
unsupported forms. Scope and evidence are recorded in
[Resolve Approvals and Structured Questions](requirements/resolve-approvals-and-structured-questions.md).

## [2026-09-18] implementation | Preserve Codex provider errors and retries

Codex `error` notifications now produce provider warnings instead of aborting
stream decoding. Native completion supplies the terminal outcome and original
error message. Regression cases use the pinned SDK notification models for
retry recovery, capacity failures, and duplicate completion delivery. See
[Provider Adapters](architecture/provider-adapters.md).

## [2026-09-19] implementation | Seed RTK rules for Grok and Muse

- Added the existing pinned RTK installer to both generated images.
- Grok and Muse join Codex in the proxy's `RTK_INIT_SPECS`: sandbox
  preparation seeds RTK's Codex rules text into `~/.grok/AGENTS.md` (Grok's
  global rules) and `~/.codex/AGENTS.md` (Muse's compatible personal rules).
  Split adapters, canonical prompts, and approval policies stay unchanged.
  Verified with headless runs of the installed grok and muse 1.3.0 CLIs that
  each file reaches the model. Prime Agent remains unchanged.
- Rules inlining is now idempotent across re-seeding: `rtk init --codex`
  re-appends its `@RTK.md` reference whenever it is missing, so the previous
  Codex inlining added a copy of the rules on every preparation.
- Inspected baseline: `95f006ecec870bd6b22549fd96755f723776970e`.
- Updated [Isolated Harness Runtimes](capabilities/isolated-harness-runtimes.md)
  and the deployment guide.

## Related

- [Wiki index](index.md)
- [Wiki maintenance](operations/wiki-maintenance.md)

## [2026-09-20] implementation | Project sandbox policies

Record the requested policy boundaries, worktree implementation, tests, and
passing create/resume compatibility checks for all seven installed providers. See [Project sandbox policies](requirements/project-sandbox-policies.md).

## [2026-09-20] repair | Sandbox review fixes

Against baseline `bd5ffc2c6887ee9ef6354d4d8d84254acd1d5be4`, separate Cursor
API-key login from refresh, persist exchanged tokens only in gateway state,
resolve readiness by the full persisted sandbox identity, and repair partial
gateway network attachment and replacement. The proxy suite passes 849 tests
with 91.27% coverage. The real Docker gate also passes after recording daemon
bind identities so Docker Desktop path translation does not replace the agent
during gateway recovery. See [Project sandbox policies](requirements/project-sandbox-policies.md).

## [2026-09-20] repair | Read-only readiness and credential mount reconciliation

Against baseline `bd5ffc2c6887ee9ef6354d4d8d84254acd1d5be4`, replace the readiness
spawn gate with adapters bound to persisted running endpoints. Health failures
cannot trigger image builds or container preparation. Include the resolved
host credential source in gateway reconciliation so a directory change cannot
retain the previous credential mount. Updated
[Project sandbox policies](requirements/project-sandbox-policies.md),
[ASGI readiness](requirements/host-django-asgi-with-readiness.md), the index,
and HTTP map. The proxy suite passes 853 tests with 91.28% coverage.

## [2026-09-22] update | MCP credential relay for policy sandboxes

Against baseline `81457b75e19d83c6f6840b9f1790fd52ac5cce33`, admit MCP header
credentials for policy sandboxes by giving the agent opaque gateway URLs and
relaying them through a Unix socket to a host-side relay that restores the
stored URL and headers. URL-embedded credentials remain rejected. Updated
[Project sandbox policies](requirements/project-sandbox-policies.md) and
[Probe and configure harnesses](requirements/probe-and-configure-harnesses.md).
Gateway reconciliation now reads the container's recorded image id; resolving
a deleted image raised `NotFound` and made preparation recreate a gateway that
still existed. The proxy suite passes 871 tests with 91.41% coverage.

## [2026-09-23] repair | Codex Git worktree permissions

Against baseline `81457b75e19d83c6f6840b9f1790fd52ac5cce33`, allow repository
metadata writes through a profile extending Codex's workspace sandbox. Preserve
other modes, approvals, protected agent directories, and Project isolation.
Record the approved request and actual sandbox test evidence in
[Provider adapters](architecture/provider-adapters.md), the compatibility map,
and the index.

## [2026-09-23] repair | Retain provider warnings in the proxy

Against baseline `81457b75e19d83c6f6840b9f1790fd52ac5cce33`, add the existing
provider warning payload to the dispatcher's streaming-event allowlist. Codex
retry warnings no longer fail a turn as unsupported events. Record regression
evidence in [Provider adapters](architecture/provider-adapters.md) and the
compatibility map.

## [2026-09-25] repair | Recover lost worker leases

Against baseline `3643af2`, a worker that loses its lease reacquires it from
the heartbeat, recovers as at startup, and resumes claims unless shutdown began
meanwhile; the runtime manager's `close_all` tears runtimes down without the
one-way shutdown. Until then a started service refuses new commands with the
dedicated `503 worker_unavailable` and the renewal interval as `Retry-After`.
Record the behavior and test evidence in
[Runtime isolation architecture](architecture/runtime-isolation.md).
