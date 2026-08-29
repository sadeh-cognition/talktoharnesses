---
type: journey
title: Host Django and Run a Conversation
status: implemented
audiences:
  - product
  - developer
tags:
  - type/journey
  - capability/http
  - capability/conversations
  - status/implemented
last_verified: 2026-08-30
verified_against_commit: 2920f5820783245bd5b871e61edd44642ebfe56a
---

# Host Django and Run a Conversation

This journey traces host composition through token issuance, harness probe, conversation create, turn submit, and SSE replay.

## 1. Compose the host

The host adds `talktoharnesses.django`, sets `TALKTOHARNESSES_JWT_SIGNING_KEY`, includes `/api/v1/`, wraps ASGI with `talktoharnesses_lifespan`, and runs migrations; harness kinds need no enablement configuration because the proxy spawns each kind's managed Docker sandbox on demand. Requirement: [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md).

## 2. Authenticate

The host issues a JWT with `issue_token(user)` or, when the standard Django
admin is installed, selects an active user in the TTH API tokens admin. The raw
admin-issued token appears only on the immediate result page. Clients send
`Authorization: Bearer`. Requirement: [Authenticate with JWT](../requirements/authenticate-with-jwt.md).

## 3. Create and probe a harness

The client creates a harness with kind and working directory, then probes. The proxy calls the configured split, which reports models and modes from its live CLI or SDK. Requirement: [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md).

## 4. Create a conversation and submit a turn

The client creates a conversation on that harness and posts a prompt with an idempotency key. Requirement: [Create and manage conversations](../requirements/create-and-manage-conversations.md), [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md).

## 5. Stream events

The client opens `GET /conversations/{id}/events` and reconnects with `Last-Event-ID` set to the last conversation sequence.

## Related

- [Product overview](../maps/product-overview.md)
- [Official HTTP client](../capabilities/official-http-client.md)
- [Python application facade](../interfaces/python-application-facade.md)
