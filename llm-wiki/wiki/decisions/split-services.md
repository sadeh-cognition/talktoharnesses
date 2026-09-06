---
type: decision
title: Split Services Architecture
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-09-06
verified_against_commit: 1655a774b7b6f7f88b56d75497277dadcaa10c30
sources:
  - raw/product/split-service-runtime-ownership.md
---

# Split Services Architecture

The monolith was split at the `HarnessAdapter` seam in commit `4764402`.

## Decision

- `talktoharnesses` becomes **tth-proxy**: client-facing API, persistence,
  auth, orchestration, and generic remote-adapter lifecycle. Provider adapters,
  probes, and compatibility logic live in the splits. The client-facing
  `/api/v1` contract and `client.py` are unchanged.
- Each harness kind lives in a top-level project directory (`tth-grok`, `tth-cursor`,
  `tth-codex`, `tth-claude`, `tth-opencode`, `tth-prime-agent`): a thin
  Django + Ninja service exposing the adapter operations as an identical
  HTTP+SSE API (`/v1/probe`, `/v1/sessions`, per-session turns/steer/
  interrupt/answers/terminate, and one SSE event stream), running in a
  a Docker sandbox the proxy spawns on demand, tracks in its database, and reuses.
- **tth-types** is the single shared package and holds schemas only: the wire
  models, enums, `DomainError` contract, adapter protocol, process events, and
  the split API bodies/frames. Non-schema code shared between splits (ACP
  JSON-RPC machinery, the process supervisor, path checks) is duplicated into
  each split that needs it; a second shared package was explicitly rejected.
  `scripts/check_split_drift.py` (run by the static CI gate) diffs every
  vendored module across the splits after normalizing the per-split package,
  kind, and executable tokens, so the copies cannot drift silently.
  `tth-prime-agent` speaks JSONL rather than ACP JSON-RPC and vendors only the
  frame decoder from the ACP package.

## Consequences and accepted trade-offs

- The proxy drives every kind through one generic `RemoteHarnessAdapter`
  (httpx + SSE). Native-dedupe deltas ride on each SSE frame so
  `export_seen` stays an in-memory call; seen sets are imported into the
  split before `resume` so replay dedupe happens at the source.
- The split's supervised process is mirrored by `RemoteProcessHandle`
  (process SSE frames; terminate over HTTP), keeping the runtime manager's
  lifecycle pump and process-record persistence.
- The split event stream has no replay: a dropped stream ends the runtime and
  the proxy recovers by native resume against a fresh split session.
- Split sessions are in-memory; a split restart loses them by design.
- The proxy↔split link uses a shared-secret header (`X-TTH-Split-Token`),
  not JWT; the proxy is a trusted client and receives raw error messages.
- Version advisories are computed by the split at probe time; the proxy caches
  them in-process, so capability reads report no advisory after a proxy
  restart until the next probe.
- Each split owns its compatibility JSON. The workspace-level
  `scripts/render_supported.py` aggregates those documents into
  `SUPPORTED_HARNESSES.md`; the proxy package contains no renderer.
- Sandbox lifecycle can contain narrowly scoped credential setup required for
  deployment: per-kind host credential files are seeded into the managed home
  volume at container creation. This is not provider adapter or protocol logic.
- Executable-scoped approval rules still resolve against the proxy host
  filesystem, while launch snapshots record container paths; such rules only
  match when the path exists identically on both sides.
- Working directories must live under the bind-mounted projects dir
  (identical path on host and container).

## Related

- [Runtime isolation decision](runtime-isolation.md)
- [Runtime isolation architecture](../architecture/runtime-isolation.md)
- [System context](../architecture/system-context.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Approved split runtime ownership](../../raw/product/split-service-runtime-ownership.md)
