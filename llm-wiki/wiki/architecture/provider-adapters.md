---
type: architecture
title: Provider Adapters
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-09-18
verified_against_commit: d337f342d5fe7bb427ad5235880c1a1aca09f677
---

# Provider Adapters

Each kind lives in its own split project (`tth-<kind>`) under `tth_<kind>.harness` with adapter, probe, compatibility, and control modules; Grok and Cursor carry their own copies of the ACP machinery. The proxy keeps the `HarnessAdapter` protocol re-export, registry, generic `RemoteHarnessAdapter`, and split lifecycle configuration.

The uncommitted ACP correlation fix discards and logs replies without a live
request, including duplicate replies and replies arriving after local cancellation.
Such replies cannot resolve another request or abort the connection's pending
turns. Request IDs retain exact string/integer matching; matching remote errors
still fail their own request, and malformed frames still fail the connection.
This prevents an uncorrelated Grok reply from ending an active workflow turn;
the reason Grok emitted the original reply remains unverified.
Implementation evidence: `tth-grok/src/tth_grok/acp/connection.py` and its
synchronized Cursor copy. Test evidence: both splits' `tests/acp/test_connection.py`
and `tth-grok/tests/harness/test_adapter.py`, including successful terminal and
usage events after an injected unmatched response.

Adapters normalize native streams into `HarnessEvent` and
`HarnessInteractionRequest`. Provider token accounting is normalized into
turn-scoped `usage_updated` events without inventing omitted categories. The
Cursor adapter accepts ACP usage shapes, but verified Cursor Agent releases do
not emit them. Capability flags are adapter-owned and copied onto probed
identities. The proxy registry constructs a remote adapter per kind; each split
constructs its own adapter per session.

Live gates prove create, resume, meaningful token usage, and advertised
capabilities against the packaged floor through the official HTTP client, with
the kind's split running in its on-demand Docker sandbox. The
Cursor gate currently exposes the upstream ACP token-usage gap rather than
passing it.

Muse Code uses the official MSP v1 command plane over `muse serve`. Its
self-contained split is `tth-muse`; shared wire models remain in `tth-types`.
Its Python connection follows Meta's SDK command identities, acknowledgment
checks, and non-admission retries. Approval delivery shares the first decision's
outcome across concurrent calls and replay.
Implementation evidence: `tth-muse/src/tth_muse/harness/`. Protocol fixtures and
adapter tests: `tth-muse/tests/test_muse.py`.

Codex's MCP tool confirmation elicitations are parsed in its adapter-owned
schemas and normalized into existing approval interactions. Responses use the
MCP `action` and `content` fields instead of command/file approval `decision`.
Implementation: `tth-codex/src/tth_codex/harness/`. Regression evidence:
`tth-codex/tests/harness/test_mcp_approvals.py`. Supported request shapes and
remaining gaps are recorded in
[Resolve approvals and structured questions](../requirements/resolve-approvals-and-structured-questions.md).

Codex SDK `error` notifications retain their native message as provider warnings.
`willRetry` selects `provider_retry` or `provider_error`; neither notification
ends the active turn. The native `turn/completed` notification determines the
outcome, including the provider's terminal error message. This prevents a model
capacity error from being replaced by an unsupported-notification failure and
allows native retries to finish. Implementation evidence:
`tth-codex/src/tth_codex/harness/{adapter,schemas,normalizer}.py`.
Test evidence: `tth-codex/tests/harness/test_error_notifications.py` exercises
the pinned SDK's slotted notifications, recovery, terminal errors, and duplicate
completion delivery.

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)
