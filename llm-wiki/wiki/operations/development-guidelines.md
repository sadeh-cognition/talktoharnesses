---
type: operation
title: Development Guidelines
status: maintained
audiences:
  - developer
tags:
  - type/operation
  - audience/developer
last_verified: 2026-08-20
verified_against_commit: bb3d2b755500fc663816d6cbd1a7cd7947a8920b
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

## Related

- [Engineering development source](../../raw/engineering/development-guidelines.md)
- [Testing guidelines](testing-guidelines.md)
- [Layered architecture](../architecture/layered-architecture.md)
