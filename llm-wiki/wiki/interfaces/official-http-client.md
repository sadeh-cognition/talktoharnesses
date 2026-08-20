---
type: interface
title: Official HTTP Client Interface
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

# Official HTTP Client Interface

`AsyncTalkToHarnessesClient` mirrors the Django HTTP/SSE surface. Install `talktoharnesses[client]`. `base_url` must include `/api/v1/`.

Public exports are `APIError`, `AsyncTalkToHarnessesClient`, and `ConversationStreamItem`. Stream items are conversation events, snapshots, or sync projections.

The client is the supported remote boundary for HTTP consumers.

## Related

- [Official HTTP client](../capabilities/official-http-client.md)
- [HTTP and SSE API](http-and-sse-api.md)
- [Engineering HTTP client source](../../raw/engineering/http-client.md)
