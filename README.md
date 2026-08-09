# talktoharnesses

Unified coding-agent harness interface with an optional Django application
surface. One distribution exposes five adapters (Grok, Cursor, Codex, Claude
Code, OpenCode), a persistence-backed asynchronous facade, and authenticated
HTTP/SSE APIs.

Accepted architectural decisions live under [`docs/adr/`](docs/adr/). Exact
create/resume support claims are generated in
[`SUPPORTED_HARNESSES.md`](SUPPORTED_HARNESSES.md). Operational detail lives in:

- [`docs/deployment.md`](docs/deployment.md)
- [`docs/upgrading.md`](docs/upgrading.md)
- [`docs/live-testing.md`](docs/live-testing.md)
- [`docs/releasing.md`](docs/releasing.md)
- [`docs/performance.md`](docs/performance.md)
- [`docs/search-retention-transcripts.md`](docs/search-retention-transcripts.md)

## Install

Requires Python 3.11+.

```bash
# Core library
pip install talktoharnesses

# Django application surface (SQLite needs FTS5; no database extra)
pip install "talktoharnesses[django]"

# PostgreSQL multi-worker profile
pip install "talktoharnesses[django,postgres]"

# Individual provider extras
pip install "talktoharnesses[grok]"      # marker only; external grok executable
pip install "talktoharnesses[cursor]"    # marker only; external cursor executable
pip install "talktoharnesses[codex]"     # pinned openai-codex SDK
pip install "talktoharnesses[claude]"    # pinned claude-agent-sdk
pip install "talktoharnesses[opencode]"  # httpx client; external opencode executable

# Full surface
pip install "talktoharnesses[all]"
```

With [uv](https://docs.astral.sh/uv/):

```bash
uv add talktoharnesses
uv add "talktoharnesses[django,postgres]"
uv add "talktoharnesses[all]"
```

Grok, Cursor, and OpenCode executables are external. The package never discovers,
installs, upgrades, or invents arbitrary flags for them. Provider SDK/executable
versions are accepted only when listed in the generated compatibility matrix for
the current operation and platform.

OpenTelemetry's API is a core dependency and is a no-op without host
configuration. Install and configure your own SDK/exporter packages separately;
there is no package-owned `otel` extra.

## Quick start (Django)

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "talktoharnesses.django",
    # ...
]
TALKTOHARNESSES_JWT_SIGNING_KEY = "replace-with-a-secret-at-least-32-bytes"
```

```python
# host/asgi.py
from django.core.asgi import get_asgi_application
from talktoharnesses.django.asgi import talktoharnesses_lifespan

application = talktoharnesses_lifespan(get_asgi_application())
```

```python
# host/urls.py
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("talktoharnesses.django.api.urls")),
]
```

```bash
python manage.py migrate
uvicorn host.asgi:application --host 127.0.0.1
```

Issue tokens in-process with `talktoharnesses.django.auth.issue_token(user)`.
The JWT signing key must be at least 32 bytes and must not equal `SECRET_KEY`.

Authenticated submissions execute local harnesses with the Django OS user's
workspace access. This is not a sandbox.

## Development

```bash
uv sync --extra django
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --cov=talktoharnesses --cov-fail-under=91
uv lock
```

## Build

```bash
uv build --no-sources
```

## Versioning

Versions use CalVer (`YYYY.M.PATCH`). Pre-releases remain `*.devN` until the
stable Phase 12 publication gate passes.
