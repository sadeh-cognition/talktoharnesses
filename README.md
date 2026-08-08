# talktoharnesses

Unified coding-agent harness interface with an optional Django application
scaffold. Pre-release (`*.devN`): domain models, pure transitions, adapter
contracts, and Django-free process/runtime supervision (`talktoharnesses.runtime`)
are available; no harness adapters or execution facade yet.

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

### ASGI / API (django extra)

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
uvicorn host.asgi:application --host 127.0.0.1
```

Required setting: `TALKTOHARNESSES_JWT_SIGNING_KEY` (≥32 bytes, must not equal
`SECRET_KEY`). Authentication does not sandbox harness execution — authorized
turn submitters run local programs as the Django OS user.

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
