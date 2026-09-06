---
type: operation
title: Development Guidelines
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-09-06
verified_against_commit: 1655a774b7b6f7f88b56d75497277dadcaa10c30
---

# Development Guidelines

After completing code changes, run `make lint` and resolve any reported issues before handing off. Typical local checks:

```bash
uv sync --extra django --extra client
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -n auto --maxprocesses=4 --dist=worksteal \
  --ignore=tests/live --ignore=tests/performance \
  --cov=talktoharnesses --cov-fail-under=91
```

Build with `uv build --no-sources`. Public `__all__` surfaces are contract-tested. Core packages must import without Django.

Code vendored into the `tth-<kind>` splits (ACP transport, process supervisor,
path checks, the split HTTP surface) must stay identical across splits. The
static gate runs `python scripts/check_split_drift.py`; when you change one
copy, re-sync the others in the same change.

## Related

- [Engineering development source](../../raw/engineering/development-guidelines.md)
- [Testing guidelines](testing-guidelines.md)
- [Layered architecture](../architecture/layered-architecture.md)
