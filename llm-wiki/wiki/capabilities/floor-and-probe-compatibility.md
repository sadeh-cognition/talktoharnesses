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
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Floor-and-Probe Compatibility

Each harness packages one floor identity, published platforms, and adapter-owned capability flags. Probe rejects identities older than the floor or on an unpublished platform. Newer identities are accepted.

## Product value

Consumers get a hard contract without a patch grid. A new CLI patch above the floor does not require a compatibility JSON edit to run. `SUPPORTED_HARNESSES.md` is generated from packaged data. `latest_verified` is advisory (`verified`, `behind_verified`, `ahead_of_verified`, `unknown`) and never fails a probe.

## Current implementation

Packaged JSON under `src/talktoharnesses/data/compatibility/` stores floors and last-verified notes. Probe copies adapter-owned flags onto the live identity. Resume is claimed only when the live agent advertises session loading. Missing extras, malformed version output, protocol mismatch, and unsupported operations fail closed.

## Requirements

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)

## Related

- [Floor-and-probe compatibility decision](../decisions/floor-and-probe-compatibility.md)
- [Strict compatibility decision](../decisions/strict-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
- [Provider adapters](../architecture/provider-adapters.md)
