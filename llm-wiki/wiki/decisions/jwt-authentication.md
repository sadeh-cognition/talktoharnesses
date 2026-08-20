---
type: decision
title: JWT Authentication Decision
status: implemented
audiences:
  - developer
tags:
  - type/decision
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/engineering/adr-0005-jwt-authentication.md
---

# JWT Authentication Decision

JWT bearer authentication is the only domain-endpoint scheme. Use HS256 with a dedicated signing key separate from Django `SECRET_KEY`. Store only a hashed `jti`, allow one active token per Django user, and default expiry to 30 days.

Cookies, CSRF login flows, and package-owned user management are out of scope. Health, readiness, and OpenAPI documentation remain unauthenticated.

## Related

- [ADR 0005 source](../../raw/engineering/adr-0005-jwt-authentication.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md)
