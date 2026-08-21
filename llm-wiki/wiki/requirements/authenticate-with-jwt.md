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
last_verified: 2026-08-21
verified_against_commit: 7cb2e2c82909ebe01fc3eb68220d7764adab64bd
sources:
  - raw/engineering/adr-0005-jwt-authentication.md
  - raw/product/django-admin-client-token-issuance.md
---

# Authenticate with JWT

## Intent

JWT bearer authentication is the only domain-endpoint authentication scheme.
Trusted hosts can provision initial client tokens in-process or through the
standard Django admin. Cookies, CSRF login for domain endpoints, and
package-owned user management are out of scope.

## Current behavior

HS256 tokens use `TALKTOHARNESSES_JWT_SIGNING_KEY`, which must be at least 32
bytes and must not equal Django `SECRET_KEY`. Only a hashed `jti` is stored. One
active token per Django user. Default expiry is 30 days. Rotate and revoke
endpoints exist. Hosts with the standard Django admin installed can select an
active user and issue its token; the raw token is shown on a non-cacheable,
one-time result page. All bearer failures return the same generic 401. Health,
readiness, and OpenAPI docs remain unauthenticated.

## Gap

No gap remains against ADR 0005.

## Acceptance criteria

- Domain endpoints reject missing, expired, revoked, and malformed bearer tokens with identical 401 bodies.
- Issuance is in-process via `issue_token`.
- An authorized Django administrator can issue a token for an active Django user
  without exposing the token digest or adding a remote issuance endpoint.
- The admin issuance response shows the raw token once and prevents response
  caching.
- Rotate replaces the active token; revoke invalidates it.
- Owner id is derived only from the authenticated user.

## Implementation evidence

- `src/talktoharnesses/django/auth.py`
- `src/talktoharnesses/django/admin.py`
- `src/talktoharnesses/django/templates/admin/talktoharnesses/apitoken/issue_token.html`
- `src/talktoharnesses/django/api/auth.py`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/unit/django/test_auth.py`
- `tests/unit/django/test_admin.py`
- `tests/e2e/test_phase5_api_gate.py`
- `tests/test_docs_ops.py::test_readme_django_setup_snippet_executes`

## Related

- [JWT authentication decision](../decisions/jwt-authentication.md)
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md)
- [Host Django ASGI with readiness](host-django-asgi-with-readiness.md)
