---
type: map
title: Compatibility and Adapters
status: maintained
audiences:
  - developer
tags:
  - type/map
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Compatibility and Adapters

Compatibility is a packaged floor plus live probe. Adapters must not claim operations they do not implement.

## Providers

Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent each have an adapter, packaged floor JSON, and live gate. Models, modes, and efforts come from the installed CLI.

## Capability flags

Resume, interrupt, steer, multi-interaction, and nested activity are adapter-owned flags. Resume is claimed only when the live agent advertises session loading. `latest_verified` is advisory.

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Floor-and-probe compatibility decision](../decisions/floor-and-probe-compatibility.md)
- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
