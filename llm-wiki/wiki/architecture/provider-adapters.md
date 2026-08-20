---
type: architecture
title: Provider Adapters
status: implemented
audiences:
  - developer
tags:
  - type/architecture
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Provider Adapters

Each kind lives under `talktoharnesses.providers.<kind>` with adapter, probe, compatibility, and control modules. Several providers share ACP types under `providers/acp`.

Adapters normalize native streams into `HarnessEvent` and `HarnessInteractionRequest`. Capability flags are adapter-owned and copied onto probed identities. The default registry constructs all six adapters.

Live gates prove create, resume, and advertised capabilities against the packaged floor through the official HTTP client.

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
