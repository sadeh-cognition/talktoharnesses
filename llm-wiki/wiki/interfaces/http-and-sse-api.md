---
type: interface
title: HTTP and SSE API
status: implemented
audiences:
  - developer
tags:
  - type/interface
  - capability/http
  - status/implemented
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# HTTP and SSE API

Django Ninja serves the versioned API at `/api/v1`. Schemas live with the routes. Handlers call `TalkToHarnessesService` and do not own harness processes.

## Unauthenticated

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Database and service readiness |
| `GET` | `/docs`, `/openapi.json` | OpenAPI |

## Authenticated clusters

Harnesses, conversations, turns, queue, steer, interrupt, switch, interactions, approval rules, audits, search, retention, transcript export/import, token rotate/revoke, and `GET /conversations/{id}/events` SSE.

SSE uses `text/event-stream`, `Last-Event-ID` as conversation sequence, `Cache-Control: no-cache`, and `X-Accel-Buffering: no`.

## Related

- [HTTP API map](../maps/http-api.md)
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md)
- [Official HTTP client interface](official-http-client.md)
- [Python application facade](python-application-facade.md)
