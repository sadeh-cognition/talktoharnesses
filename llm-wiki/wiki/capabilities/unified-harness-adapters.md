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
last_verified: 2026-08-30
verified_against_commit: 78003994d9fe93108ce5a6bc3591ab2e2ef904d9
---

# Unified Harness Adapters

TalkToHarnesses drives Grok, Cursor, Codex, Claude Code, OpenCode, and Prime Agent through one adapter protocol. Provider-specific types do not leak into the facade, HTTP API, or domain models.

## Product value

[Target users](../concepts/target-users.md) select a harness kind, working directory, and optional model, mode, and effort. The package accepts identities at or above the packaged floor for the current platform. Models, modes, and efforts come from the live CLI.

Each kind runs in a separate split service. Process-bound splits locate their external CLI on the split's PATH or from its `TALKTOHARNESSES_*_EXECUTABLE` override; Codex and Claude splits pin their SDK dependencies. The proxy never installs, upgrades, or constructs provider command lines.

## Current implementation

Each split implements `HarnessAdapter` with probe, start, resume, submit, steer, interrupt, answer_interaction, events, and close. The proxy registry constructs the same `RemoteHarnessAdapter` for all six kinds. Cursor model selectors use the string `model` field (`model-id[key=value,...]`). `yolo: true` suppresses approval prompts through provider-native mechanisms.

Adapters map provider-reported token counts into the canonical `usage_updated`
event before successful turn completion. Categories remain absent when the
provider does not report them. Cursor's ACP adapter supports the canonical
mapping, but verified Cursor Agent releases currently omit the native usage
data, so Cursor turns do not emit `usage_updated`.

## Requirements

- [Probe and configure harnesses](../requirements/probe-and-configure-harnesses.md)
- [Steer, interrupt, and switch harness](../requirements/steer-interrupt-and-switch-harness.md)
- [Report harness token usage](../requirements/report-harness-token-usage.md)

## Related

- [Adapter protocol](../interfaces/adapter-protocol.md)
- [Provider adapters](../architecture/provider-adapters.md)
- [Floor-and-probe compatibility](floor-and-probe-compatibility.md)
- [Compatibility and adapters](../maps/compatibility-and-adapters.md)
