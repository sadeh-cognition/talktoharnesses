---
type: requirement
title: Authenticate with JWT
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/http
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0005-jwt-authentication.md
---

# Authenticate with JWT

## Intent

JWT bearer authentication is the only domain-endpoint authentication scheme. Cookies, CSRF login, and package-owned user management are out of scope.

## Current behavior

HS256 tokens use `TALKTOHARNESSES_JWT_SIGNING_KEY`, which must be at least 32 bytes and must not equal Django `SECRET_KEY`. Only a hashed `jti` is stored. One active token per Django user. Default expiry is 30 days. Rotate and revoke endpoints exist. All bearer failures return the same generic 401. Health, readiness, and OpenAPI docs remain unauthenticated.

## Gap

No gap remains against ADR 0005.

## Acceptance criteria

- Domain endpoints reject missing, expired, revoked, and malformed bearer tokens with identical 401 bodies.
- Issuance is in-process via `issue_token`.
- Rotate replaces the active token; revoke invalidates it.
- Owner id is derived only from the authenticated user.

## Implementation evidence

- `src/talktoharnesses/django/auth.py`
- `src/talktoharnesses/django/api/auth.py`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/unit/django/test_auth.py`
- `tests/e2e/test_phase5_api_gate.py`
- `tests/test_docs_ops.py::test_readme_django_setup_snippet_executes`

## Related

- [JWT authentication decision](../decisions/jwt-authentication.md)
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md)
- [Host Django ASGI with readiness](host-django-asgi-with-readiness.md)
