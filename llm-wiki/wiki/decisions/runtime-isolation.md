---
type: decision
title: Runtime Isolation Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-08-29
verified_against_commit: 47644027875773ba520cbfdd9f978d196a548802
sources:
  - raw/engineering/adr-0003-runtime-isolation.md
  - raw/product/split-service-runtime-ownership.md
---

# Runtime Isolation Decision

Create one supervised SDK or process runtime per active conversation. Request handlers never own its lifetime. Disconnecting the last client does not interrupt work. The split service owns the native runtime; the proxy mirrors its lifecycle remotely. Managed Docker splits do not run as child harness processes of the Django proxy.

SQLite uses a single-supervisor profile. PostgreSQL may coordinate multiple workers through claims and leases without transferring a live stdio connection. No external broker is added.

## Related

- [ADR 0003 source](../../raw/engineering/adr-0003-runtime-isolation.md)
- [Runtime isolation architecture](../architecture/runtime-isolation.md)
- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Approved split runtime ownership](../../raw/product/split-service-runtime-ownership.md)
