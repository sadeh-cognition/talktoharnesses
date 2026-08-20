---
type: capability
title: Isolated Harness Runtimes
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/runtime
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Isolated Harness Runtimes

Each active conversation owns one supervised SDK or process runtime. Request handlers never own its lifetime. Disconnecting the last client does not interrupt work.

## Product value

Turns outlive HTTP requests. Harnesses run locally as the Django OS user with that user's workspace access. This is not a sandbox.

## Current implementation

`RuntimeManager` and `ProcessSupervisor` create, watch, and reap candidate runtimes. SQLite uses a single-supervisor profile. PostgreSQL may coordinate multiple workers through transactional claims, renewable leases, and notifications without transferring a live stdio connection.

## Requirements

- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)

## Related

- [Runtime isolation architecture](../architecture/runtime-isolation.md)
- [Runtime isolation decision](../decisions/runtime-isolation.md)
- [System context](../architecture/system-context.md)
