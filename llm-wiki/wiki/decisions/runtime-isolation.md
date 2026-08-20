---
type: decision
title: Runtime Isolation Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0003-runtime-isolation.md
---

# Runtime Isolation Decision

Create one supervised SDK or process runtime per active conversation. Request handlers never own its lifetime. Disconnecting the last client does not interrupt work. Harnesses run locally as the Django OS user.

SQLite uses a single-supervisor profile. PostgreSQL may coordinate multiple workers through claims and leases without transferring a live stdio connection. No external broker is added.

## Related

- [ADR 0003 source](../../raw/engineering/adr-0003-runtime-isolation.md)
- [Runtime isolation architecture](../architecture/runtime-isolation.md)
- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
