---
type: operation
title: Deployment
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-21
verified_against_commit: 7cb2e2c82909ebe01fc3eb68220d7764adab64bd
---

# Deployment

Host `talktoharnesses.django` behind a Django ASGI process. Required: auth/contenttypes, `TALKTOHARNESSES_JWT_SIGNING_KEY` of at least 32 bytes not equal to `SECRET_KEY`, and SQLite FTS5 or PostgreSQL.

Wrap ASGI with `talktoharnesses_lifespan`. Run `python manage.py migrate` once before starting workers. The package never auto-migrates and does not ship containers, systemd units, or reverse-proxy templates.

Initial client tokens are issued inside the trusted host. A host using the
standard Django admin can enable `/admin/` and generate a token for an active
Django user from the TTH API tokens admin. The raw value must be copied from the
immediate result page into the client's secret configuration.

SQLite is single-supervisor. PostgreSQL is the multi-worker profile. Authenticated submissions execute local harnesses as the Django OS user.

## Related

- [Engineering deployment source](../../raw/engineering/deployment.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Upgrading](upgrading.md)
- [JWT authentication decision](../decisions/jwt-authentication.md)
