---
type: capability
title: Django HTTP and SSE Surface
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/http
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Django HTTP and SSE Surface

The optional Django extra exposes a versioned Ninja API at `/api/v1` plus an SSE event stream. Route handlers are thin adapters over `TalkToHarnessesService`.

## Product value

Hosts compose Django settings, URL includes, and ASGI lifespan. Clients authenticate with JWT, submit turns, and reconnect to the stream using conversation sequence ids.

## Current implementation

`talktoharnesses.django` registers models, migrations, auth, and management commands. `talktoharnesses_lifespan` starts one process-local service. Health, readiness, and OpenAPI docs are unauthenticated. Owner isolation is enforced on every domain query.

## Requirements

- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)

## Related

- [HTTP and SSE API](../interfaces/http-and-sse-api.md)
- [HTTP API map](../maps/http-api.md)
- [Official HTTP client](official-http-client.md)
- [Deployment](../operations/deployment.md)
