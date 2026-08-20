# ADR 0007: Floor-and-probe compatibility

- **Status:** Accepted
- **Date:** 2026-08-20
- **Supersedes:** [ADR 0004](0004-strict-compatibility.md) (exact release allowlists)

## Context

Harness CLIs ship patches frequently. ADR 0004's exact `{release_id, platform}`
matrices required a packaged row for every proven patch. That churned
`SUPPORTED_HARNESSES.md` without changing adapter behavior.

Consumers still need a hard contract: do not drive CLIs the adapter cannot
handle, and do not claim operations the adapter does not implement.

## Decision

Compatibility is a **floor plus live probe**, not a patch grid.

- Each harness packages one floor identity, the platforms it may run on, and
  adapter-owned capability flags.
- Probe rejects identities older than the floor or on an unpublished platform
  (`provider_incompatible`). Newer identities are accepted.
- Models, modes, and efforts come from the live CLI. Resume is claimed only
  when the live agent advertises session loading (ACP `loadSession`). Other
  capability flags are adapter-owned and do not require a per-patch matrix row.
- `latest_verified` is advisory only (`verified`, `behind_verified`,
  `ahead_of_verified`, `unknown`). It never fails a probe.
- Bumping `latest_verified` after a live gate is optional documentation, not a
  runtime allowlist.

Strictness remains: missing extras, malformed version output, protocol
mismatch, and unsupported operations still fail closed.

## Consequences

A new CLI patch above the floor does not require a compatibility JSON edit to
run. Breaking CLIs can still fail later (unknown ACP notifications, protocol
drift); those need an adapter fix or a floor bump, not a new matrix row.
