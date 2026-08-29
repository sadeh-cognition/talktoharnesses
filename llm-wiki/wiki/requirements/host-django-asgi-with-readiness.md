---
type: requirement
title: Host Django ASGI with Readiness
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/http
  - capability/runtime
  - status/implemented
last_verified: 2026-08-30
verified_against_commit: 2920f5820783245bd5b871e61edd44642ebfe56a
sources:
  - raw/engineering/deployment.md
  - raw/product/readme.md
  - raw/product/split-service-runtime-ownership.md
---

# Host Django ASGI with Readiness

## Intent

A host application can compose Django settings, URL includes, and ASGI lifespan so one worker owns proxy startup, readiness, and shutdown. The host configures reachable split services separately. The project provides split Dockerfiles but does not auto-run migrations.

## Current behavior

Hosts add `talktoharnesses.django`, set `TALKTOHARNESSES_JWT_SIGNING_KEY`, include `/api/v1/`, and wrap ASGI with `talktoharnesses_lifespan`. Harness kinds need no enablement configuration; the proxy spawns each kind's managed Docker sandbox on demand. `GET /health` returns ok. `GET /ready` checks the database and process-local proxy service. SQLite is single-supervisor. PostgreSQL is the multi-worker profile. Migrations run once via the host.

## Gap

No gap remains against the documented host integration. Mixed-version rolling upgrades are unsupported; see [Upgrading](../operations/upgrading.md).

## Acceptance criteria

- Lifespan starts one `TalkToHarnessesService` per process.
- Readiness fails closed when the database or service is not ready.
- Health remains unauthenticated.
- API routers use `get_service()` rather than constructing a second service.

## Implementation evidence

- `src/talktoharnesses/django/asgi.py`
- `src/talktoharnesses/django/apps.py`
- `src/talktoharnesses/application/readiness.py`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/test_docs_ops.py::test_readme_django_setup_snippet_executes`
- `tests/unit/django/test_asgi.py`
- `tests/e2e/test_phase9_recovery_gate.py`
- `tests/test_django_init.py`

## Related

- [Deployment](../operations/deployment.md)
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md)
- [Isolated harness runtimes](../capabilities/isolated-harness-runtimes.md)
- [Engineering deployment source](../../raw/engineering/deployment.md)
- [Approved split runtime ownership](../../raw/product/split-service-runtime-ownership.md)
