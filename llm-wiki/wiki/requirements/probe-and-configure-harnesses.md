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
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
sources:
  - raw/product/readme.md
  - raw/engineering/adr-0007-floor-and-probe-compatibility.md
---

# Probe and Configure Harnesses

## Intent

An owner can create a named harness of a supported kind, store its configuration, probe the installed CLI against the packaged floor, and read advertised models, modes, efforts, and capability flags.

## Current behavior

`POST /harnesses` persists an owner-owned `HarnessInstance`. `POST /harnesses/{id}/probe` runs the adapter probe and returns `HarnessProbeProjection` including `VersionAdvisory`. Identities below the floor or on unpublished platforms fail with `provider_incompatible`. Models, modes, and efforts come from the live CLI. Cursor accepts `model-id[key=value,...]` selectors. `yolo` is fixed at creation.

## Gap

No gap remains against the documented floor-and-probe contract.

## Acceptance criteria

- Creating a harness stores kind, working directory, optional executable, model, mode, effort, and yolo without inventing CLI flags.
- Probe rejects identities older than the floor or on unpublished platforms.
- Probe accepts newer identities and reports an advisory vs `latest_verified`.
- Capabilities, models, and modes endpoints return the last probed or freshly probed values.
- Missing extras and malformed version output fail closed.

## Implementation evidence

- `src/talktoharnesses/application/service.py` (`create_harness`, `probe_harness`, `get_harness_capabilities`, `get_harness_models`, `get_harness_modes`)
- `src/talktoharnesses/providers/compatibility.py`
- `src/talktoharnesses/data/compatibility/*.json`
- `src/talktoharnesses/django/api/routes.py`

## Test evidence

- `tests/unit/providers/test_compatibility_matrices.py`
- `tests/unit/providers/*/test_probe.py`
- `tests/unit/providers/*/test_compatibility.py`
- `tests/live/test_*_live.py`
- `tests/test_import.py::test_supported_harnesses_markdown_drift`

## Related

- [Unified harness adapters](../capabilities/unified-harness-adapters.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Floor-and-probe compatibility decision](../decisions/floor-and-probe-compatibility.md)
- [README product source](../../raw/product/readme.md)
