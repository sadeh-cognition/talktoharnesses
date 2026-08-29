---
type: capability
title: Floor-and-Probe Compatibility
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/compatibility
  - status/implemented
last_verified: 2026-08-29
verified_against_commit: 47644027875773ba520cbfdd9f978d196a548802
---

# Floor-and-Probe Compatibility

Each harness packages one floor identity, published platforms, and adapter-owned capability flags. Probe rejects identities older than the floor or on an unpublished platform. Newer identities are accepted.

## Product value

Consumers get a hard contract without a patch grid. A new CLI patch above the floor does not require a compatibility JSON edit to run. `SUPPORTED_HARNESSES.md` is aggregated from compatibility data owned by the six split projects. `latest_verified` is advisory (`verified`, `behind_verified`, `ahead_of_verified`, `unknown`) and never fails a probe.

## Current implementation

Packaged JSON under each `tth-<kind>/src/tth_<kind>/data/compatibility/` directory stores floors and last-verified notes. The split computes the version advisory at probe and copies adapter-owned flags onto the live identity. Resume is claimed only when the live agent advertises session loading. Missing split dependencies, malformed version output, protocol mismatch, and unsupported operations fail closed.

## Requirements

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)

## Related

- [Floor-and-probe compatibility decision](../decisions/floor-and-probe-compatibility.md)
- [Strict compatibility decision](../decisions/strict-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
- [Provider adapters](../architecture/provider-adapters.md)
