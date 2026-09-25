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
last_verified: 2026-09-24
verified_against_commit: 3643af2
---

# Official HTTP Client Interface

`AsyncTalkToHarnessesClient` mirrors the Django HTTP/SSE surface. Install `talktoharnesses[client]`. `base_url` must include `/api/v1/`.

Public exports are `APIError`, `AsyncTalkToHarnessesClient`, and `ConversationStreamItem`. Stream items are conversation events, snapshots, or sync projections.

The client is the supported remote boundary for HTTP consumers.

Ordinary HTTP calls are not retried; callers own backoff. `APIError.retry_after`
exposes the response's `Retry-After` in seconds. The API sends it with
`503 worker_unavailable` while its command worker cannot take new turns,
steers, interrupts, or harness switches, so callers can retry with the same
idempotency key after that delay. The idempotent commands (`submit_turn`,
`steer`, `switch_harness`) do that themselves when the client is built with
`command_retry_seconds` (default `0`): they wait each `Retry-After` (1 s when
absent, at least 0.1 s) with the same idempotency key until the budget is
spent, then raise the last refusal; other errors are raised at once, and
`interrupt`, which has no idempotency key, is never retried.

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
`tests/unit/test_client.py` covers fixed-token behavior, `Retry-After`
parsing, and the command retry budget. Operator contract:
`docs/http-client.md`.

## Related

- [Official HTTP client](../capabilities/official-http-client.md)
- [HTTP and SSE API](http-and-sse-api.md)
- [Engineering HTTP client source](../../raw/engineering/http-client.md)
