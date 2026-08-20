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
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/deployment.md
  - raw/product/readme.md
---

# Host Django ASGI with Readiness

## Intent

A host application can compose Django settings, URL includes, and ASGI lifespan so one worker owns service startup, readiness, and shutdown. The package does not ship containers or auto-run migrations.

## Current behavior

Hosts add `talktoharnesses.django`, set `TALKTOHARNESSES_JWT_SIGNING_KEY`, include `/api/v1/`, and wrap ASGI with `talktoharnesses_lifespan`. `GET /health` returns ok. `GET /ready` checks the database and process-local service. SQLite is single-supervisor. PostgreSQL is the multi-worker profile. Migrations run once via the host.

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
