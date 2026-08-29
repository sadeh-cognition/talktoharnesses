---
type: operation
title: Upgrading
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-29
verified_against_commit: 47644027875773ba520cbfdd9f978d196a548802
---

# Upgrading

Conservative stop/migrate/start is required. Mixed-version rolling upgrades are not supported. Backward migration compatibility is not promised.

The current release resets migration history and supports only new databases. Stored harness or binding configuration JSON containing `executable_path` fails validation and must be recreated. Process-bound CLIs are located by their split service from kind. Read aggregated `SUPPORTED_HARNESSES.md` and release notes before changing the proxy or splits. Coordinate proxy, split, and caller upgrades because mixed-version rolling upgrades are unsupported.

## Related

- [Engineering upgrading source](../../raw/engineering/upgrading.md)
- [Deployment](deployment.md)
- [Releasing](releasing.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
