---
type: map
title: HTTP API Map
status: maintained
audiences:
  - developer
tags:
  - type/map
  - audience/developer
last_verified: 2026-09-02
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# HTTP API Map

The Django Ninja API is mounted at `/api/v1`. Health, readiness, and OpenAPI documentation are unauthenticated. All other routes require a JWT bearer token.

## Clusters

- Auth: rotate and revoke tokens.
- Harnesses: create, list, probe, capabilities, models, modes, delete.
- Conversations: create, list, archive, pin, snooze, soft-delete, transcript export/import.
- Turns: submit, queue edit/cancel, steer, interrupt, switch, runtime close (`POST /conversations/{id}/runtime/close` releases an idle harness process; the next turn resumes it).
- Interactions: list pending, draft, resolve.
- Approval rules and interaction audits.
- Search and retention.
- SSE event stream with `Last-Event-ID` replay, including the `workspace_setup_started` / `workspace_setup_completed` events that bracket a sandbox's repo-declared setup; a failing setup ends the turn with `workspace_setup_failed` (409 on direct HTTP surfaces).

- [Project sandbox policies](../requirements/project-sandbox-policies.md)

## Related

- [HTTP and SSE API](../interfaces/http-and-sse-api.md)
- [Django HTTP and SSE surface](../capabilities/django-http-sse.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)
- [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md)
- [Official HTTP client interface](../interfaces/official-http-client.md)
- [Provision sandbox workspaces](../requirements/provision-sandbox-workspaces.md)
