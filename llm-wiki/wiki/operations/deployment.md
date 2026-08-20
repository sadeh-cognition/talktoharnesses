---
type: operation
title: Deployment
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Deployment

Host `talktoharnesses.django` behind a Django ASGI process. Required: auth/contenttypes, `TALKTOHARNESSES_JWT_SIGNING_KEY` of at least 32 bytes not equal to `SECRET_KEY`, and SQLite FTS5 or PostgreSQL.

Wrap ASGI with `talktoharnesses_lifespan`. Run `python manage.py migrate` once before starting workers. The package never auto-migrates and does not ship containers, systemd units, or reverse-proxy templates.

SQLite is single-supervisor. PostgreSQL is the multi-worker profile. Authenticated submissions execute local harnesses as the Django OS user.

## Related

- [Engineering deployment source](../../raw/engineering/deployment.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Upgrading](upgrading.md)
- [JWT authentication decision](../decisions/jwt-authentication.md)
