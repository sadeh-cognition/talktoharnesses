---
type: architecture
title: Runtime Isolation Architecture
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-09-25
verified_against_commit: 3643af2
---

# Runtime Isolation Architecture

One managed runtime per active conversation holds native session state. HTTP handlers never own that lifetime. Durable ownership lives in the database.

The runtime's process lives in the kind's split service: the proxy's `RemoteProcessHandle` mirrors it from process SSE frames and terminates it over HTTP. The split's own supervisor keeps the previous guarantees (no shell, session groups, Windows job objects, capped redacted stderr). This runs inside the split's proxy-managed container. Its immutable project policy revision, provider, and writable mounts determine the scope. Existing sessions resume in their original scope; policy changes apply to new sessions.

SQLite deployments must run a single live proxy supervisor. PostgreSQL workers claim conversations with leases and notifications. A live split session is not transferred between workers. API and worker execution may share a process.

A worker that loses its lease, typically because the host slept past it,
stops claims and closes its runtimes without shutting the runtime manager
down. The heartbeat retries the lease each renewal interval; once reacquired,
the worker recovers expired conversations as at startup and then resumes
claims, unless shutdown began meanwhile. Until then a started service refuses
new turns, steers, interrupts, and harness switches with
`503 worker_unavailable` and a `Retry-After` of the renewal interval; replays
of accepted idempotency keys still succeed. Internal
`worker_lease_unavailable` errors keep their 409 mapping. Implementation:
`src/talktoharnesses/application/worker_coordinator.py`,
`src/talktoharnesses/runtime/manager.py` (`close_all`), and
`src/talktoharnesses/django/api/errors.py`. Tests:
`tests/unit/application/test_worker_coordinator.py` (reacquisition, shutdown
during reacquisition), `tests/unit/application/test_service.py`,
`tests/unit/django/test_api.py`, and `tests/unit/django/test_api_errors.py`.

The recorded commit is the inspected baseline; these policy boundaries are
implemented in the associated uncommitted worktree changes.

## Related

- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Runtime isolation decision](../decisions/runtime-isolation.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)

- [Project sandbox policies](../requirements/project-sandbox-policies.md)
