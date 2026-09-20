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
last_verified: 2026-09-21
verified_against_commit: 44c6324
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

HTTP and SSE use the same HTTPX authentication flow. Each connection gets its own
retry state, including after a dropped authentication retry. An optional async
`on_token_rejected(token)` callback reports the final rejected token before
callers can translate `APIError`. It requires `token_provider` and can raise an
application-specific replacement error. Stores must compare the reported token
with their current credential before changing its state.

Implementation: `src/talktoharnesses/client.py` and
`src/talktoharnesses/_client_auth.py`. Evidence:
`tests/unit/test_client_token_provider.py` uses real HTTP connections to cover
bounded retries, request preservation, stream reconnect cursors, reconnection
after a dropped authentication retry, and final-token rejection callbacks;
`tests/unit/test_client.py` covers fixed-token behavior. Operator contract:
`docs/http-client.md`.

## Related

- [Official HTTP client](../capabilities/official-http-client.md)
- [HTTP and SSE API](http-and-sse-api.md)
- [Engineering HTTP client source](../../raw/engineering/http-client.md)
