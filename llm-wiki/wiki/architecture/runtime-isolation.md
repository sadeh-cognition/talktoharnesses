---
type: architecture
title: Runtime Isolation Architecture
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Runtime Isolation Architecture

One supervised runtime per active conversation holds native session state. HTTP handlers never own that lifetime. Durable ownership lives in the database.

SQLite deployments must run a single live supervisor. PostgreSQL workers claim conversations with leases and notifications. A live stdio connection is not transferred between workers. API and worker execution may share a process.

Windows job objects group child processes. Stderr is retained up to a fixed byte cap.

## Related

- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Runtime isolation decision](../decisions/runtime-isolation.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
