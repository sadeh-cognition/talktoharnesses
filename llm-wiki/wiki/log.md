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

## Related

- [Wiki index](index.md)
- [Wiki maintenance](operations/wiki-maintenance.md)
