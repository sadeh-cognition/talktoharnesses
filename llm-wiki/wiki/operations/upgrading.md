---
type: operation
title: Upgrading
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-21
verified_against_commit: 3f90f85a37028a1ba0498cff641ef5c8a1bec6d7
---

# Upgrading

Conservative stop/migrate/start is required. Mixed-version rolling upgrades are not supported. Backward migration compatibility is not promised.

The current release resets migration history and supports only new databases. Stored harness or binding configuration JSON containing `executable_path` fails validation and must be recreated; process-bound CLIs are now located by TTH from kind. Read generated `SUPPORTED_HARNESSES.md` and release notes before changing the package. Coordinate caller and TTH upgrades because mixed-version rolling upgrades are unsupported.

## Related

- [Engineering upgrading source](../../raw/engineering/upgrading.md)
- [Deployment](deployment.md)
- [Releasing](releasing.md)
- [Floor-and-probe compatibility](../capabilities/floor-and-probe-compatibility.md)
