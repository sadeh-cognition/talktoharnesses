# Deployment and operations

Operational guide for hosting the `talktoharnesses` proxy behind a Django ASGI
process. Every enabled harness kind also needs a reachable split service. See
the canonical [`deploy/README.md`](../deploy/README.md) for URL overrides,
building the six split images, and proxy-managed Docker sandboxes. The project
does not provide systemd units, Helm charts, or reverse-proxy templates.

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

## Split services and capacity

Runtime capacity is fixed at 20 concurrent managed runtimes per process. Create
owner-scoped harness configurations through the public API/facade. Harness
configuration rejects executable paths. The proxy spawns every kind's Docker
sandbox on demand the first time its endpoint is resolved: a missing image is
built locally from the repository's per-kind build context, the container is
started and health-checked, and the sandbox is recorded in the
`talktoharnesses_sandbox` table (kind, container, image, port, base URL, split
token, status). After a proxy restart the persisted token lets the proxy
reattach to its running containers instead of recreating them. Pre-building
images with `deploy/build-splits.sh` avoids the first-use build wait; while a
build or boot is still in progress a request fails with `sandbox_preparing`
(retry shortly), and unrecoverable sandbox failures surface as
`sandbox_unavailable` with an actionable message (Docker unreachable, build
failed, missing credential file, port in use, or health timeout). Background
readiness probing never spawns or builds — it only reattaches to sandboxes
that are already running.

The per-sandbox split token is stored in the clear in the proxy database; it
guards loopback-only traffic between the proxy and its containers, which share
the same trust domain.

CLI and SDK discovery belongs to the split service. The sandbox image contains
the CLI or SDK and the proxy forwards the configured credential environment.
The proxy does not store provider credentials.

## Authentication

- Issue tokens with trusted in-process `talktoharnesses.django.auth.issue_token(user)`.
- Hosts with the standard Django admin installed can issue a token from
  `/admin/talktoharnesses/apitoken/add/`. The form selects an active Django user
  and shows the raw token only on the immediate success page.
- Treat the Django `talktoharnesses.add_apitoken` permission as highly
  privileged: it can replace the API credential of any active Django user and
  act as that user's TTH API identity.
- One active token per user; rotation/revocation invalidate prior JTIs.
- Do not commit signing keys. Do not add login, OAuth, or credential-storage flows
  in this package.
- Client authentication is separate from proxy-to-split authentication. URL
  override isolation is owned by the split operator; managed Docker splits use
  the restrictions documented in [`deploy/README.md`](../deploy/README.md).

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
- Proxy shutdown drains owned tasks; managed split containers intentionally
  remain running for reuse

## Recovery limits

Document these plainly to operators:

- Ambiguous delivery becomes `outcome_unknown` and is never retried.
- A failed worker's live process/stdio is not adopted by another worker.
- Uncommitted provider bytes may be lost.
- Native resume may fall back to a canonical retained handoff.
- SQLite has no multi-worker takeover.
- Abandoned provider-native sessions are not deleted remotely.
