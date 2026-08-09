# Deployment and operations

Single operational guide for hosting `talktoharnesses` behind a Django ASGI
process. This package documents integration points only; it does not ship
containers, systemd units, Helm charts, or reverse-proxy templates.

## Host settings

Required:

- `INSTALLED_APPS` includes `talktoharnesses.django` (plus Django auth/contenttypes).
- `TALKTOHARNESSES_JWT_SIGNING_KEY`: at least 32 bytes, must not equal `SECRET_KEY`.
- Database configured for either SQLite (FTS5 required) or PostgreSQL.

Optional:

- `TALKTOHARNESSES_TOKEN_TTL`: positive `timedelta` for issued JWT lifetime.
- Swappable Django user model is supported; `owner_id` is derived only from the
  authenticated user. Globally unique IDs never bypass owner filtering.

## Database profiles

- **SQLite**: no database extra. Requires FTS5. Strict single-live-supervisor
  deployment profile — do not run multiple workers against one SQLite database.
- **PostgreSQL**: install `talktoharnesses[django,postgres]` (Psycopg 3). This is
  the multi-worker / recommended production profile. Fencing and failover are
  package-owned; they do not make SQLite multi-worker safe.

Run migrations once before starting workers:

```bash
python manage.py migrate
```

The package never runs migrations automatically.

## ASGI composition

Wrap the Django ASGI application so one service/worker composition owns the
process lifespan:

```python
from django.core.asgi import get_asgi_application
from talktoharnesses.django.asgi import talktoharnesses_lifespan

application = talktoharnesses_lifespan(get_asgi_application())
```

Include API routes:

```python
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("talktoharnesses.django.api.urls")),
]
```

Bind Uvicorn to loopback unless the host intentionally terminates TLS elsewhere:

```bash
uvicorn host.asgi:application --host 127.0.0.1
```

Lifespan startup failure is reported to the ASGI server so traffic is not served
without a worker. Use:

- `GET /api/v1/health` — liveness
- `GET /api/v1/ready` — generic readiness (fails closed when lifespan/startup is incomplete)

Graceful termination drains owned runtimes within the shared shutdown budget.

## Capacity and harness configuration

Runtime capacity is fixed at 20 concurrent managed runtimes per process. Create
owner-scoped harness configurations through the public API/facade. Executable
paths and working directories / additional roots are ownership-checked. Provider
authentication is inherited from the service OS environment; the package does
not store provider credentials.

Grok, Cursor, and OpenCode require explicit executable paths. Codex and Claude
use their pinned SDK extras; Claude may use a bundled or explicit CLI path.

## Authentication

- Issue tokens with trusted in-process `talktoharnesses.django.auth.issue_token(user)`.
- One active token per user; rotation/revocation invalidate prior JTIs.
- Do not commit signing keys. Do not add login, OAuth, or credential-storage flows
  in this package.
- Authenticated submissions execute local harnesses as the Django OS user and are
  not a sandbox.

## Retention cleanup

Schedule externally:

```bash
python manage.py talktoharnesses_cleanup
# Read-only aggregate preview across owners:
python manage.py talktoharnesses_cleanup --dry-run
```

Retention uses each owner's configured calendar-month policy (default six months).
Workspace files and provider-native sessions are never deleted by this command.
See [`search-retention-transcripts.md`](search-retention-transcripts.md) for
policies, exemptions, preview, ranked search, and transcript import/export.

## Observability

`opentelemetry-api` is a core dependency and remains a no-op without host
configuration. Install and configure your chosen SDK and exporters in the host
process. Instrumentation stays secret-safe and low-cardinality; there is no
package-owned SDK, exporter, collector, or `otel` extra.

## Operator checks

- Database connectivity and migrations applied
- `/health` and `/ready` after lifespan start
- Configured harness probes succeed for intended platforms
- Logs/metrics destination owned by the host
- Backup of the relational database before upgrades
- Process termination leaves no owned child processes

## Recovery limits

Document these plainly to operators:

- Ambiguous delivery becomes `outcome_unknown` and is never retried.
- A failed worker's live process/stdio is not adopted by another worker.
- Uncommitted provider bytes may be lost.
- Native resume may fall back to a canonical retained handoff.
- SQLite has no multi-worker takeover.
- Abandoned provider-native sessions are not deleted remotely.
