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
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Host Django and Run a Conversation

This journey traces host composition through token issuance, harness probe, conversation create, turn submit, and SSE replay.

## 1. Compose the host

The host adds `talktoharnesses.django`, sets `TALKTOHARNESSES_JWT_SIGNING_KEY`, includes `/api/v1/`, wraps ASGI with `talktoharnesses_lifespan`, and runs migrations. Requirement: [Host Django ASGI with readiness](../requirements/host-django-asgi-with-readiness.md).

## 2. Authenticate

The host issues a JWT with `issue_token(user)`. Clients send `Authorization: Bearer`. Requirement: [Authenticate with JWT](../requirements/authenticate-with-jwt.md).

## 3. Create and probe a harness

The client creates a harness with kind and working directory, then probes. Models and modes come from the live CLI. Requirement: [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md).

## 4. Create a conversation and submit a turn

The client creates a conversation on that harness and posts a prompt with an idempotency key. Requirement: [Create and manage conversations](../requirements/create-and-manage-conversations.md), [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md).

## 5. Stream events

The client opens `GET /conversations/{id}/events` and reconnects with `Last-Event-ID` set to the last conversation sequence.

## Related

- [Product overview](../maps/product-overview.md)
- [Official HTTP client](../capabilities/official-http-client.md)
- [Python application facade](../interfaces/python-application-facade.md)
