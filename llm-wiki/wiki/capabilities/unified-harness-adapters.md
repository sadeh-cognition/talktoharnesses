---
type: capability
title: Unified Harness Adapters
status: implemented
audiences:
  - product
  - developer
tags:
  - type/capability
  - capability/adapters
  - status/implemented
last_verified: 2026-08-21
verified_against_commit: c996cbcd23b7cbf4f6b4d70422ab17ce715661bf
---

# Unified Harness Adapters

TalkToHarnesses drives Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent through one adapter protocol. Provider-specific types do not leak into the facade, HTTP API, or domain models.

## Product value

[Target users](../concepts/target-users.md) select a harness kind, working directory, and optional model, mode, and effort. The package accepts identities at or above the packaged floor for the current platform. Models, modes, and efforts come from the live CLI.

Grok, Cursor, OpenCode, and Prime Agent executables are external CLIs located on PATH (or a TalkToHarnesses process environment override) at probe and launch. Codex and Claude extras pin SDKs. The package never installs, upgrades, or invents arbitrary flags.

## Current implementation

Each provider implements `HarnessAdapter` with probe, start, resume, submit, steer, interrupt, answer_interaction, events, and close. A default registry constructs the six adapters. Cursor model selectors use the string `model` field (`model-id[key=value,...]`). `yolo: true` suppresses approval prompts through provider-native mechanisms.

Adapters map provider-reported token counts into the canonical `usage_updated`
event before successful turn completion. Categories remain absent when the
provider does not report them.

## Requirements

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Steer, interrupt, and switch harness](../requirements/steer-interrupt-and-switch-harness.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)

## Related

- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Floor-and-probe compatibility](floor-and-probe-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
