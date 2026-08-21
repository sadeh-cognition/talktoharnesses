---
type: requirement
title: Probe and Configure Harnesses
status: implemented
audiences:
  - product
  - developer
tags:
  - type/requirement
  - capability/adapters
  - status/implemented
last_verified: 2026-08-21
verified_against_commit: 3f90f85a37028a1ba0498cff641ef5c8a1bec6d7
sources:
  - raw/product/readme.md
  - raw/product/tth-owned-harness-executable-discovery-requirements.md
  - raw/engineering/adr-0007-floor-and-probe-compatibility.md
---

# Probe and Configure Harnesses

## Intent

An owner can create a named harness of a supported kind, store its configuration, probe the installed CLI against the packaged floor, and read advertised models, modes, efforts, and capability flags.

## Current behavior

`POST /harnesses` persists an owner-owned `HarnessInstance` with kind, working directory, workspace roots, and optional model, mode, effort, and yolo. The create body and domain configuration reject executable paths. For Grok, Cursor, OpenCode, and Prime Agent, probe and launch locate the conventional CLI on PATH, or from a TalkToHarnesses process environment override (`TALKTOHARNESSES_GROK_EXECUTABLE` and the matching Cursor/OpenCode/Prime Agent names). An invalid override fails without falling back to PATH. Codex and Claude use their bundled SDKs. `POST /harnesses/{id}/probe` returns `HarnessProbeProjection` including `VersionAdvisory`. Identities below the floor or on unpublished platforms fail with `provider_incompatible`. Models, modes, and efforts come from the live CLI. Cursor accepts `model-id[key=value,...]` selectors. `yolo` is fixed at creation. Historical stored JSON containing `executable_path` fails validation and must be recreated.

## Gap

No gap remains against the documented floor-and-probe contract.

## Acceptance criteria

- Creating a harness stores kind, working directory, workspace roots, optional model, mode, effort, and yolo without an executable path or invented CLI flags.
- HTTP, direct domain construction, and stored configuration reject `executable_path`.
- Process-bound probe and launch resolve the executable from the TTH environment override or conventional name on TTH's PATH.
- Probe rejects identities older than the floor or on unpublished platforms.
- Probe accepts newer identities and reports an advisory vs `latest_verified`.
- Capabilities, models, and modes endpoints return the last probed or freshly probed values.
- Missing extras and malformed version output fail closed.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (`create_harness`, `probe_harness`, `get_harness_capabilities`, `get_harness_models`, `get_harness_modes`)
- `src/talktoharnesses/runtime/paths.py` (`resolve_kind_executable`)
- `src/talktoharnesses/providers/grok/probe.py`, `providers/cursor/probe.py`, `providers/opencode/probe.py`, `providers/prime_agent/probe.py`
- `src/talktoharnesses/runtime/manager.py` (`_plan_launch`)
- `src/talktoharnesses/domain/models.py` (`HarnessConfiguration`)
- `src/talktoharnesses/providers/compatibility.py`
- `src/talktoharnesses/data/compatibility/*.json`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/unit/providers/test_compatibility_matrices.py`
- `tests/unit/providers/*/test_probe.py`
- `tests/unit/providers/*/test_compatibility.py`
- `tests/runtime/test_paths.py` (`resolve_kind_executable`, strict legacy `executable_path` rejection)
- `tests/unit/django/test_api.py` (`test_create_harness_rejects_executable_path`)
- `tests/live/test_*_live.py`
- `tests/test_import.py::test_supported_harnesses_markdown_drift`

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Floor-and-probe compatibility decision](../decisions/floor-and-probe-compatibility.md)
- [README product source](../../raw/product/readme.md)
- [Approved executable-discovery requirements](../../raw/product/tth-owned-harness-executable-discovery-requirements.md)
