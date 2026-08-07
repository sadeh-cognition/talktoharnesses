# talktoharnesses

Unified coding-agent harness interface with an optional Django application
scaffold. The current release is a pre-release packaging baseline: no public
runtime API or executable entry point is provided yet.

Accepted architectural decisions live under [`docs/adr/`](docs/adr/).

## Install

Requires Python 3.11+.

```bash
# Core library (Pydantic only)
pip install talktoharnesses

# Optional Django application surface
pip install "talktoharnesses[django]"
```

With [uv](https://docs.astral.sh/uv/):

```bash
uv add talktoharnesses
uv add "talktoharnesses[django]"
```

Django app path for host projects: `talktoharnesses.django`.

## Development

```bash
uv sync --extra django   # package + dev tools + Django extra
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --cov=talktoharnesses
uv lock                  # refresh lockfile after pyproject edits
```

## Build

```bash
uv build --no-sources
```

## Versioning

Versions remain pre-releases (`*.devN`) until the five-harness milestone is
complete. Versioning follows CalVer (`YYYY.M.PATCH`).
