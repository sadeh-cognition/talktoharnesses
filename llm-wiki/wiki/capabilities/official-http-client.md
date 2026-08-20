---
type: capability
title: Official HTTP Client
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

# Official HTTP Client

The `client` extra ships `AsyncTalkToHarnessesClient`, a hand-written async client for the Django HTTP/SSE surface. `base_url` must be the mounted versioned API root and must include `/api/v1/`.

## Product value

Remote consumers such as Agentbahn call TalkToHarnesses without depending on Django internals. The client covers harnesses, conversations, turns, interactions, search, retention, transcripts, and SSE replay.

## Current implementation

The client uses httpx, domain Pydantic models, and an SSE decoder. Importing it without the extra raises a documented `ModuleNotFoundError`.

## Requirements

- [Submit turns and stream events](../requirements/submit-turns-and-stream-events.md)
- [Authenticate with JWT](../requirements/authenticate-with-jwt.md)

## Related

- [Official HTTP client interface](../interfaces/official-http-client.md)
- [HTTP and SSE API](../interfaces/http-and-sse-api.md)
- [Engineering HTTP client source](../../raw/engineering/http-client.md)
