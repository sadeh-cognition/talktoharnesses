---
type: decision
title: Floor-and-Probe Compatibility Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0007-floor-and-probe-compatibility.md
---

# Floor-and-Probe Compatibility Decision

Compatibility is a floor plus live probe, not a patch grid. Each harness packages one floor identity, platforms, and adapter-owned capability flags. Probe rejects identities older than the floor or on an unpublished platform. Newer identities are accepted.

Models, modes, and efforts come from the live CLI. Resume is claimed only when the live agent advertises session loading. `latest_verified` is advisory and never fails a probe. This supersedes [strict compatibility](strict-compatibility.md) exact allowlists.

## Related

- [ADR 0007 source](../../raw/engineering/adr-0007-floor-and-probe-compatibility.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Strict compatibility decision](strict-compatibility.md)
- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
