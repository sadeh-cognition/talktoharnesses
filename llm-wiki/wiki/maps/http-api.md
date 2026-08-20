---
type: map
title: HTTP API Map
status: maintained
audiences:
  - developer
tags:
  - type/map
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# HTTP API Map

The Django Ninja API is mounted at `/api/v1`. Health, readiness, and OpenAPI documentation are unauthenticated. All other routes require a JWT bearer token.

## Clusters

- Auth: rotate and revoke tokens.
- Harnesses: create, list, probe, capabilities, models, modes, delete.
- Conversations: create, list, archive, pin, snooze, soft-delete, transcript export/import.
- Turns: submit, queue edit/cancel, steer, interrupt, switch.
- Interactions: list pending, draft, resolve.
- Approval rules and interaction audits.
- Search and retention.
- SSE event stream with `Last-Event-ID` replay.

## Related

- [HTTP and SSE API](../interfaces/http-and-sse-api.md)
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Official HTTP client interface](../interfaces/official-http-client.md)
