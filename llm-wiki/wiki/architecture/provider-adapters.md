---
type: architecture
title: Provider Adapters
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-09-05
verified_against_commit: 92bdf81138628204f7b58df5f1f80545abdbbde3
---

# Provider Adapters

Each kind lives in its own split project (`tth-<kind>`) under `tth_<kind>.harness` with adapter, probe, compatibility, and control modules; Grok and Cursor carry their own copies of the ACP machinery. The proxy keeps the `HarnessAdapter` protocol re-export, registry, generic `RemoteHarnessAdapter`, and split lifecycle configuration.

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

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)
