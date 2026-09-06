---
type: domain
title: Harness Instance
status: implemented
audiences:
  - product
  - developer
tags:
  - type/domain
  - capability/adapters
last_verified: 2026-09-06
verified_against_commit: d55a5a38d39b80e5f755d419b52b680bb1c08347
---

# Harness Instance

A harness instance is an owner-owned named configuration: kind, working directory, workspace roots, optional model, mode, effort, and yolo, and optional streamable HTTP MCP servers (`mcp_servers`) that supporting splits attach to every session. Create and stored configuration reject executable paths. The proxy resolves the kind to a remote split; that split owns CLI or SDK discovery, probe, and launch. A `LaunchSnapshot` records the executable selected by the split.

Probe produces `HarnessCapabilities` and a `VersionAdvisory`. Switching harnesses creates a new binding rather than mutating the old one.

## Related

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Conversation](conversation.md)
- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
