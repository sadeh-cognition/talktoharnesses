# talktoharnesses

Unified coding-agent harness interface with an optional Django application
surface. One distribution exposes six adapters (Grok, Cursor, Codex, Claude
Code, OpenCode, Prime Agent), a persistence-backed asynchronous facade, and authenticated
HTTP/SSE APIs.

Accepted architectural decisions live under [`docs/adr/`](docs/adr/). Floor
identities, adapter-owned capabilities, and last-verified notes are generated in
[`SUPPORTED_HARNESSES.md`](SUPPORTED_HARNESSES.md). Operational detail lives in:

- [`docs/deployment.md`](docs/deployment.md)
- [`docs/upgrading.md`](docs/upgrading.md)
- [`docs/live-testing.md`](docs/live-testing.md)
- [`docs/releasing.md`](docs/releasing.md)
- [`docs/performance.md`](docs/performance.md)
- [`docs/search-retention-transcripts.md`](docs/search-retention-transcripts.md)
- [`docs/http-client.md`](docs/http-client.md)

## Install

Requires Python 3.11+.

```bash
# Core library
pip install talktoharnesses

# Django application surface (SQLite needs FTS5; no database extra)
pip install "talktoharnesses[django]"

# PostgreSQL multi-worker profile
pip install "talktoharnesses[django,postgres]"

# Official async HTTP client
pip install "talktoharnesses[client]"

# Individual provider extras
pip install "talktoharnesses[grok]"      # marker only; external grok executable
pip install "talktoharnesses[cursor]"    # marker only; external cursor executable
pip install "talktoharnesses[codex]"     # pinned openai-codex SDK
pip install "talktoharnesses[claude]"    # pinned claude-agent-sdk
pip install "talktoharnesses[opencode]"  # httpx client; external opencode executable
pip install "talktoharnesses[prime-agent]" # marker only; external prime-agent executable

# Full surface
pip install "talktoharnesses[all]"
```

With [uv](https://docs.astral.sh/uv/):

```bash
uv add talktoharnesses
uv add "talktoharnesses[django,postgres]"
uv add "talktoharnesses[all]"
```

Grok, Cursor, OpenCode, and Prime Agent executables are external. The package never discovers,
installs, upgrades, or invents arbitrary flags for them. Provider SDK/executable
versions are accepted when they meet the packaged compatibility floor for the
current platform. Models, modes, and efforts come from the live CLI.

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

## Cursor model selectors

Cursor model parameters use the existing string-valued `model` field; there is
no separate provider-neutral parameter object. The accepted forms are:

```text
model-id
model-id[key=value,...]
```

For example, a Cursor harness can set its session baseline with:

```json
{
  "kind": "cursor",
  "executable_path": "/path/to/cursor-agent",
  "working_directory": "/workspace",
  "model": "composer-2.5[fast=false]",
  "mode": "ask"
}
```

The `model` field on `POST /conversations/{conversation_id}/turns` accepts the
same syntax for a one-turn override:

```json
{
  "prompt": "Review this change",
  "model": "gpt-5.6-sol[context=272k,reasoning=high,fast=false]"
}
```

Parameter names and values are case-sensitive, model-specific, and validated
against the options advertised by the active Cursor release. Whitespace around
the selector, IDs, and values is ignored; duplicate keys, empty keys or values,
and commas or brackets inside values are invalid. `auto` selects Cursor's
default model. Parameters omitted from a selector retain the values Cursor
advertises after selecting that model.

`HarnessConfiguration.model` establishes the baseline for create and resume.
After a turn-level override, the next turn without a `model` restores that
baseline. Model selectors do not change the separately configured Cursor
workflow `mode` (`agent`, `plan`, or `ask`). Invalid selectors fail before the
prompt is sent.

Set `yolo: true` on a harness to suppress approval prompts through
provider-native mechanisms. No approval interaction or audit is published,
and package allow/deny rules do not participate. Structured questions remain
interactive. Yolo does not change model, workflow mode, workspace roots,
sandbox selection, or provider hard denials. It is fixed at harness creation
and applies to both new and resumed sessions.

## Development

```bash
uv sync --extra django --extra client
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -n auto --maxprocesses=4 --dist=worksteal \
  --ignore=tests/live --ignore=tests/performance \
  --cov=talktoharnesses --cov-fail-under=91
uv lock
```

## Build

```bash
uv build --no-sources
```

## Versioning

Versions use CalVer (`YYYY.M.PATCH`). Pre-releases remain `*.devN` until the
stable Phase 12 publication gate passes.
