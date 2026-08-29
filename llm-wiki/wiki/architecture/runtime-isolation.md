---
type: architecture
title: Runtime Isolation Architecture
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-30
verified_against_commit: 2920f5820783245bd5b871e61edd44642ebfe56a
---

# Runtime Isolation Architecture

One managed runtime per active conversation holds native session state. HTTP handlers never own that lifetime. Durable ownership lives in the database.

The runtime's process lives in the kind's split service: the proxy's `RemoteProcessHandle` mirrors it from process SSE frames and terminates it over HTTP. The split's own supervisor keeps the previous guarantees (no shell, session groups, Windows job objects, capped redacted stderr). This runs inside the split's proxy-managed container.

SQLite deployments must run a single live proxy supervisor. PostgreSQL workers claim conversations with leases and notifications. A live split session is not transferred between workers. API and worker execution may share a process.

## Related

- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Runtime isolation decision](../decisions/runtime-isolation.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
