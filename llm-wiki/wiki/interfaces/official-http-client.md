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
last_verified: 2026-09-20
verified_against_commit: 5adaa86
---

# Official HTTP Client Interface

`AsyncTalkToHarnessesClient` mirrors the Django HTTP/SSE surface. Install `talktoharnesses[client]`. `base_url` must include `/api/v1/`.

Public exports are `APIError`, `AsyncTalkToHarnessesClient`, and `ConversationStreamItem`. Stream items are conversation events, snapshots, or sync projections.

The client is the supported remote boundary for HTTP consumers.

An optional async `token_provider` resolves a shared credential before each HTTP
request and SSE connection. A 401 reloads it and retries once only when it changed,
preserving bodies, idempotency keys, and stream cursors. Fixed-token behavior is
unchanged. Provider clients reject direct rotation/revocation, which belongs to
the caller's credential store. The two constructor options are mutually exclusive.

Implementation: `src/talktoharnesses/client.py`. Evidence:
`tests/unit/test_client_token_provider.py` uses real HTTP connections to cover
bounded retries, request preservation, and stream reconnect cursors;
`tests/unit/test_client.py` covers fixed-token behavior. Operator contract:
`docs/http-client.md`.

## Related

- [Official HTTP client](../capabilities/official-http-client.md)
- [HTTP and SSE API](http-and-sse-api.md)
- [Engineering HTTP client source](../../raw/engineering/http-client.md)
