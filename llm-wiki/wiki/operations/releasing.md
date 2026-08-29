---
type: operation
title: Releasing
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-29
verified_against_commit: 47644027875773ba520cbfdd9f978d196a548802
---

# Releasing

Versions use CalVer (`YYYY.M.PATCH`). Pre-releases remain `*.devN` until the stable publication gate passes. The checklist lives in repository `docs/releasing.md` and `scripts/ci/stable_cut_checklist.sh`.

Gates include static checks, coverage, live create/resume/interaction proof against each split-owned floor, and a floor/platform row for every adapter. `uv run python scripts/render_supported.py --validate stable --check` validates the workspace aggregate. The package never contains credentials or a mutable patch allowlist.

## Related

- [Engineering releasing source](../../raw/engineering/releasing.md)
- [Testing guidelines](testing-guidelines.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
- [Upgrading](upgrading.md)
