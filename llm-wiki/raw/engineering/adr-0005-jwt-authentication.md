# ADR 0005: JWT Authentication

- **Status:** Accepted
- **Date:** 2026-08-07

## Context

The HTTP API is multi-user and browser clients must authenticate streaming
requests with bearer headers. Cookies, CSRF login flows, and package-owned user
management are outside the package scope.

## Decision

JWT bearer authentication is the only domain-endpoint authentication scheme.
Use HS256 with a dedicated required signing key separate from Django's
`SECRET_KEY`. Store only a hashed `jti`, allow one active token per Django user,
and default expiry to 30 days with embedding-application configuration.

## Consequences

Phase 0 does not add JWT packages or middleware. The auth phase adds Python
issuance, authenticated HTTP rotation, revocation, generic failure responses,
and owner-scoped ORM access. Only health, readiness, and OpenAPI documentation
remain unauthenticated.
