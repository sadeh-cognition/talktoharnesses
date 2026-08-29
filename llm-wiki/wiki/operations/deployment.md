---
type: operation
title: Deployment
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-30
verified_against_commit: 2920f5820783245bd5b871e61edd44642ebfe56a
---

# Deployment

Host `talktoharnesses.django` behind a Django ASGI process. Required: auth/contenttypes, `TALKTOHARNESSES_JWT_SIGNING_KEY` of at least 32 bytes not equal to `SECRET_KEY`, and SQLite FTS5 or PostgreSQL.

Wrap ASGI with `talktoharnesses_lifespan`. Run `python manage.py migrate` once before starting workers. The package never auto-migrates. The repository provides six split Dockerfiles and a build script, but no systemd units, Helm charts, or reverse-proxy templates.

Initial client tokens are issued inside the trusted host. A host using the
standard Django admin can enable `/admin/` and generate a token for an active
Django user from the TTH API tokens admin. The raw value must be copied from the
immediate result page into the client's secret configuration.

SQLite is single-supervisor. PostgreSQL is the multi-worker profile. Harness kinds need no enablement configuration: the proxy spawns each kind's Docker sandbox on demand, builds a missing image locally, records the sandbox and its split token in the database, and reattaches to running containers after a restart. CLI/SDK discovery and provider authentication belong to the split. Managed Docker splits use the proxy's documented container restrictions.

## Related

- [Engineering deployment source](../../raw/engineering/deployment.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Upgrading](upgrading.md)
- [JWT authentication decision](../decisions/jwt-authentication.md)
- [Split services decision](../decisions/split-services.md)
- [Approved split runtime ownership](../../raw/product/split-service-runtime-ownership.md)
