---
type: architecture
title: Provider Adapters
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-21
verified_against_commit: c996cbcd23b7cbf4f6b4d70422ab17ce715661bf
---

# Provider Adapters

Each kind lives under `talktoharnesses.providers.<kind>` with adapter, probe, compatibility, and control modules. Several providers share ACP types under `providers/acp`.

Adapters normalize native streams into `HarnessEvent` and `HarnessInteractionRequest`. Provider token accounting is normalized into turn-scoped `usage_updated` events without inventing omitted categories. Capability flags are adapter-owned and copied onto probed identities. The default registry constructs all six adapters.

Live gates prove create, resume, meaningful token usage, and advertised capabilities against the packaged floor through the official HTTP client.

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)
