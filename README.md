# talktoharnesses (TTH)

TalkToHarnesses (TTH) is a unified coding-agent harness interface with an optional Django application
surface. One distribution exposes six adapters (Grok, Cursor, Codex, Claude
Code, OpenCode, Prime Agent), a persistence-backed asynchronous facade, and authenticated
HTTP/SSE APIs.

Accepted architectural decisions live under [`docs/adr/`](docs/adr/). Floor
identities, adapter-owned capabilities, and last-verified notes are generated in
[`SUPPORTED_HARNESSES.md`](SUPPORTED_HARNESSES.md). The Obsidian knowledge graph
lives in [`llm-wiki/`](llm-wiki/). Operational detail lives in:

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

Grok, Cursor, OpenCode, and Prime Agent executables are external. At probe and
launch, TalkToHarnesses locates each conventional executable on its process
PATH, after checking the matching `TALKTOHARNESSES_*_EXECUTABLE` environment
override. Harness configuration does not accept an executable path. The
package never installs, upgrades, or invents arbitrary flags for external
CLIs. Provider SDK/executable versions are accepted when they meet the
packaged compatibility floor for the current platform. Models, modes, and
efforts come from the live CLI.

OpenTelemetry's API is a core dependency and is a no-op without host
configuration. Install and configure your own SDK/exporter packages separately;
there is no package-owned `otel` extra.

## Quick start (Django)

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "talktoharnesses.django",
    # ...
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
STATIC_URL = "static/"
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
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("talktoharnesses.django.api.urls")),
]
```

```bash
python manage.py migrate
uvicorn host.asgi:application --host 127.0.0.1
```

### Provision a client token

TTH does not expose a login or initial token-issuance endpoint. An operator must
issue the initial token inside the trusted TTH Django host and provide it to the
remote client through its secret configuration. For example, with Django's
default user model, create a dedicated client user and print its token:

```bash
python manage.py shell -c '
from django.contrib.auth import get_user_model
from talktoharnesses.django.auth import issue_token_sync

user, _ = get_user_model().objects.get_or_create(username="example-client")
print(issue_token_sync(user).token)
'
```

Treat the printed value as a secret. Pass it as a bearer token:

```bash
export TTH_API_TOKEN="replace-with-the-issued-token"
curl -H "Authorization: Bearer ${TTH_API_TOKEN}" \
  http://127.0.0.1:8000/api/v1/harnesses
```

Or provide it to the official HTTP client:

```python
import os

from talktoharnesses.client import AsyncTalkToHarnessesClient


async def list_harnesses():
    async with AsyncTalkToHarnessesClient(
        "http://127.0.0.1:8000/api/v1/",
        token=os.environ["TTH_API_TOKEN"],
    ) as client:
        return await client.list_harnesses(limit=100)
```

Applications embedded in the TTH Django host can instead call
`talktoharnesses.django.auth.issue_token(user)` directly. Only one token is
active per Django user, so issuing another token invalidates the previous one.
An authenticated client can use `rotate_token()`, but must persist the returned
replacement token itself. The JWT signing key must be at least 32 bytes and
must not equal `SECRET_KEY`.

When the standard Django admin is installed, TTH also registers an **API
tokens** admin. Create a host administrator with `python manage.py
createsuperuser`, then open `/admin/talktoharnesses/apitoken/add/` to select an
active Django user and generate its client token. The raw token appears only on
the immediate success page; copy it into the client's secret configuration
before leaving the page.

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
