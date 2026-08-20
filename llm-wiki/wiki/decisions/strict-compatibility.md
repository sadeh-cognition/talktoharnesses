---
type: decision
title: Strict Compatibility Decision
status: historical
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
  - status/historical
last_verified: 2026-08-20
sources:
  - raw/engineering/adr-0004-strict-compatibility.md
---

# Strict Compatibility Decision

ADR 0004 required strict, explicit compatibility and deferred public manifests until later phases. It is superseded by [floor-and-probe compatibility](floor-and-probe-compatibility.md).

Exact `{release_id, platform}` allowlists churned generated docs without changing adapter behavior. Strict fail-closed behavior remains: adapters must not claim unsupported harnesses or operations.

## Related

- [ADR 0004 source](../../raw/engineering/adr-0004-strict-compatibility.md)
- [Floor-and-probe compatibility decision](floor-and-probe-compatibility.md)
- [Requirements by status](../maps/requirements-by-status.md)
