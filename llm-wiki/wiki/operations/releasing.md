---
type: operation
title: Releasing
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
---

# Releasing

Versions use CalVer (`YYYY.M.PATCH`). Pre-releases remain `*.devN` until the stable publication gate passes. The checklist lives in repository `docs/releasing.md` and `scripts/ci/stable_cut_checklist.sh`.

Gates include static checks, coverage, live create/resume/interaction proof against the packaged floor, and a floor/platform row for every adapter. The package never contains credentials or a mutable patch allowlist.

## Related

- [Engineering releasing source](../../raw/engineering/releasing.md)
- [Testing guidelines](testing-guidelines.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Upgrading](upgrading.md)
