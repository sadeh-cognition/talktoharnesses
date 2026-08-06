# Plan: `talktoharnesses_django` — reusable Django app over django-ninja

## Context

`talktoharnesses` is a Python port of [T3 Code](https://github.com/pingdotgg/t3code)'s provider layer:
one `Harness` Protocol, one canonical `RuntimeEvent` union, five drivers. It is a library only —
there is no server.

T3 itself pairs that provider layer with `apps/server`, a Node/Bun backend. Verified from the
source: T3's harness surface is **WebSocket RPC, not REST** (HTTP serves only static assets,
attachments, environment metadata, an OTLP proxy). It is **event-sourced CQRS** — clients dispatch
commands (`thread.turn.start`, `thread.approval.respond`, …) → commands emit events
(`thread.turn-start-requested`, `thread.activity-appended`, …) → events project into a read model
persisted in SQLite. Clients `subscribeThread` and receive monotonic push events. On top of the
harness slice T3 also owns projects, threads, git worktrees, terminals, and preview.

So the user's assumption is correct, with one correction acted on here: T3's harness API is
command/event-sourced, and its server is much broader than harness control. This plan ports T3's
**orchestration slice** — projects, threads, commands, event log, read model — to a reusable Django
app, exposed over **django-ninja HTTP + SSE**.

Outcome: a host Django project `pip install talktoharnesses[server]`, adds one app to
`INSTALLED_APPS`, includes one URLconf, and gets a multi-user, persistent, streaming agent backend.

### Decisions taken (settled with the user)

1. **In-process by default, ASGI-only.** The app owns harnesses in an in-memory registry on the
   server's event loop. This is the default backend and the dev/test path.
2. **SSE via django-ninja.** Async endpoints returning `StreamingHttpResponse`. No django-channels.
3. **Full event log + read model.** Append-only event table projected into Django models — replay,
   reconnect-with-cursor, history APIs, Django admin.
4. **Scope includes thread/project orchestration**, not just raw harness control.
5. **Horizontal scale is in scope, not deferred.** The plan ships a split-tier topology — stateless
   web workers plus dedicated harness worker processes — behind two seams: `HarnessBackend`
   (in-process vs sidecar) and `EventBroadcast` (in-process vs polling/Postgres/Redis). See §10.
   Because there are now two real implementations of each, the seams are load-bearing rather than
   speculative, and they are introduced at M3/M4 so the sidecar is a drop-in rather than a rewrite.

### Two library facts that drive the design (verified in source, not assumed)

**(a) `stream_events()` subscribes lazily — events are silently dropped.**
`drivers/claude.py:226`, `drivers/codex.py:266`, `drivers/opencode.py:251`, `acp/runtime.py:290`
are all `def stream_events(self): return self._stream_events()`, where `_stream_events`'s first line
is `q = self._bus.subscribe()`. An async generator's body does not run until the first
`__anext__()`. `EventBus` (`_event_bus.py`) has no replay buffer. So if the supervisor calls
`start_session()` before the pump task has actually begun iterating, `session.started` and
`thread.started` are lost. **Fixed in M0.**

**(b) Abandoning the `send_turn()` generator kills the turn on 3 of 5 drivers.**
`drivers/claude.py:219-223` and `acp/runtime.py:284-287` cancel the in-flight prompt task in the
generator's `finally`. The generator's lifetime *is* the turn's lifetime for claude/cursor/grok.
It must never be owned by an HTTP request — a client disconnect would cancel the turn.

Corollary: `await anext(gen)` is not a portable "fire the turn" primitive either.
`drivers/codex.py:213` awaits `turn_start` *before* the first yield; `drivers/opencode.py:215`
issues the prompt POST *after* it. Only iterating to exhaustion is portable.

Together these force the core runtime shape: **a supervisor-owned task per turn drives
`send_turn()` to exhaustion and discards its events; a separate long-lived pump on
`stream_events()` is the sole DB writer.** Both read the same `EventBus`, and every event
`send_turn` yields is also published to the bus (`_emit(started); yield started`), so discarding the
turn stream loses nothing.

---

## 1. Packaging

A second hatch package in this repo, same distribution, gated behind a `django` extra. The app is
tightly coupled to the `RuntimeEvent` union (which will grow); one repo, one lockfile, one
mypy/ruff config. Nothing under `talktoharnesses/` ever imports `talktoharnesses_django`, and
`talktoharnesses_django/__init__.py` contains **no Django imports** — so the pure-library install is
untouched. Keep the directory self-contained so a later move to a uv workspace is a pure `git mv`.

```toml
# pyproject.toml
[project.optional-dependencies]
django = ["django>=5.0", "django-ninja>=1.4"]
server = ["talktoharnesses[all,django]"]

[tool.hatch.build.targets.wheel]
packages = ["src/talktoharnesses", "src/talktoharnesses_django"]

[tool.mypy]
packages = ["talktoharnesses", "talktoharnesses_django"]
plugins = ["mypy_django_plugin.main"]

[tool.django-stubs]
django_settings_module = "tests.django.settings"

[dependency-groups]
dev = [..., "django>=5.0", "django-ninja>=1.4", "pytest-django>=4.9",
       "django-stubs[compatible-mypy]>=5.0"]
```

Python package `talktoharnesses_django`; Django `label = "talktoharnesses"`; explicit
`db_table = "tth_*"` on every model so a relabel never forces a migration.

```python
# apps.py
class TalkToHarnessesConfig(AppConfig):
    name = "talktoharnesses_django"
    label = "talktoharnesses"
    default_auto_field = "django.db.models.BigAutoField"
    def ready(self) -> None:
        from talktoharnesses_django import checks, conf  # noqa: F401
        conf.get_settings()              # ImproperlyConfigured early
        ensure_drivers_loaded()          # registry.py
```
`ready()` must **not** start the supervisor — it also runs under `migrate`/`collectstatic` and in
every process.

**Settings.** One dict `TALKTOHARNESSES = {...}`, parsed into a pydantic model in `conf.py`
(pydantic is already a hard dep; no new validation library):

```python
class TthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    DEFAULT_HARNESS: str = "codex"
    HARNESS_KWARGS: dict[str, dict[str, Any]] = {}   # server-side only; see §6
    ALLOWED_WORKSPACE_ROOTS: list[Path] = []         # empty == deny all
    ALLOW_ANY_WORKSPACE: bool = False
    ALLOW_ELEVATED_RUNTIME_MODES: bool = False
    AUTH: str = "talktoharnesses_django.auth.django_session_auth"
    PERMISSION_HOOK: str | None = None
    QUERYSET_FILTER: str | None = None
    IDLE_TIMEOUT_SECONDS: float = 900.0
    MAX_CONCURRENT_HARNESSES: int = 8
    TURN_START_TIMEOUT_SECONDS: float = 30.0
    SSE_KEEPALIVE_SECONDS: float = 15.0
    SSE_BACKLOG_LIMIT: int = 2000
    SSE_REPLAY_PAGE_SIZE: int = 500
    EVENT_BATCH_MAX: int = 64
    EVENT_BATCH_INTERVAL_MS: int = 25
    PERSIST_RAW: bool = True
    RETAIN_EVENTS_DAYS: int | None = None
    WORKER_ID: str | None = None      # default f"{hostname}:{pid}:{uuid4()}"
    # --- topology (§10) ---
    HARNESS_BACKEND: str = "inprocess"      # "inprocess" | "sidecar"
    BROADCAST_BACKEND: str = "inprocess"    # "inprocess" | "polling" | "postgres" | "redis"
    BROADCAST_POLL_MS: int = 250
    WORKER_ENDPOINT: str | None = None      # advertised addr: unix:///... or tcp://host:port
    WORKER_LISTEN: str | None = None        # what `manage.py tth_worker` binds
    WORKER_HEARTBEAT_SECONDS: float = 5.0
    WORKER_HEARTBEAT_TIMEOUT: float = 20.0
    WORKER_MAX_THREADS: int = 8             # per harness worker
    WORKER_RPC_TIMEOUT: float = 30.0
```
`conf.get_settings()` caches; a `setting_changed` receiver clears it so `override_settings` works.

**Mounting.** Ship both, document the ready-made `NinjaAPI` as the default:

```python
# api.py
router = Router(tags=["talktoharnesses"])                  # composed of sub-routers
def register_exception_handlers(api: NinjaAPI) -> None: ...
api = NinjaAPI(title="talktoharnesses", version="1.0.0",
               urls_namespace="talktoharnesses", auth=resolve_auth())
api.add_router("/", router); register_exception_handlers(api)
# urls.py
urlpatterns = [path("", api.urls)]
```
Host either `path("api/agents/", include("talktoharnesses_django.urls"))` or
`their_api.add_router("/agents/", tth_router)` — **the second path must also call
`register_exception_handlers(their_api)`**, because django-ninja registers handlers per `NinjaAPI`,
not per `Router`. Real footgun; document loudly.

---

## 2. Module layout

```
src/talktoharnesses_django/
  __init__.py            # __version__ only; no django imports
  apps.py  conf.py  checks.py  urls.py  api.py  auth.py  errors.py  enums.py
  models/      __init__.py  events.py  projects.py  threads.py  content.py
  schemas/     __init__.py  commands.py  read.py  events.py  common.py
  domain/      events.py            # 28 orchestration event payload models
               sequence.py          # allocate_sequences(kind, id, n) -> int
  commands/    dispatch.py          # dispatch(cmd, ctx) -> DispatchResult (sync)
               handlers.py          # @handles("thread.create") ...
  projections/ __init__.py  apply.py  thread.py  message.py  activity.py
               plan.py  checkpoint.py  session.py  runtime.py
  runtime/     supervisor.py  pump.py  turn.py  hub.py  lifespan.py  workspace.py
               backends/   __init__.py  base.py  inprocess.py  sidecar.py
               broadcast/  __init__.py  base.py  inprocess.py  polling.py
                           postgres.py  redis.py
               worker/     server.py  routing.py  heartbeat.py  drain.py
  routers/     meta.py  projects.py  threads.py  turns.py  requests.py
               events_sse.py  commands.py
  sse.py  admin.py  migrations/0001_initial.py
  management/commands/tth_gc_events.py  tth_status.py  tth_worker.py
```

**Reuse, do not redefine.** django-ninja 1.x accepts plain `pydantic.BaseModel` for bodies and
`response=`, so `Capabilities`, `SendTurnInput`, `SessionStartInput`, `Session`, `TokenUsage`,
`PlanStep`, `DiffHunk` (`src/talktoharnesses/types.py`) and the `RuntimeEvent` union
(`src/talktoharnesses/events.py`) are used **directly**. The union renders in OpenAPI as a
discriminated `oneOf` — free documentation of every SSE payload. `runtime_event_to_dict()` is the
SSE serializer; `parse_runtime_event()` the inverse. New output schemas use `ninja.ModelSchema` over
the read-model models. `extra="forbid"` on the existing models is exactly the right posture for
request bodies (see §6.2).

---

## 3. Domain model ported from T3

Aggregates `project` and `thread`. Append-only `tth_event` with per-aggregate monotonic `sequence`.

**Client commands:** `project.create|meta.update|delete`;
`thread.create|delete|archive|unarchive|settle|unsettle|snooze|unsnooze|pin|unpin|meta.update`;
`thread.runtime-mode.set`, `thread.interaction-mode.set`, `thread.turn.start`,
`thread.turn.interrupt`, `thread.approval.respond`, `thread.user-input.respond`,
`thread.checkpoint.revert`, `thread.session.stop`.
**Internal commands:** `thread.session.set`, `thread.message.assistant.delta|complete`,
`thread.proposed-plan.upsert`, `thread.turn.diff.complete`, `thread.activity.append`,
`thread.revert.complete`, `thread.title.regeneration.complete`.

**Events** (28): `project.created|meta-updated|deleted`; `thread.created|deleted|archived|
unarchived|settled|unsettled|snoozed|unsnoozed|pinned|unpinned|meta-updated|runtime-mode-set|
interaction-mode-set|message-sent|turn-start-requested|turn-interrupt-requested|
approval-response-requested|user-input-response-requested|checkpoint-revert-requested|reverted|
session-stop-requested|session-set|proposed-plan-upserted|turn-diff-completed|activity-appended`.
Every event carries `sequence, event_id, aggregate_kind, aggregate_id, occurred_at, command_id,
causation_event_id, correlation_id, metadata`.

**Read model** (Django models): `Project`; `Thread` (title, model_selection, runtime_mode,
interaction_mode, branch, worktree_path, archived_at, settled_override/settled_at, snoozed_until/
snoozed_at, pinned_at, deleted_at); `ThreadSession` (status `idle|starting|running|ready|
interrupted|stopped|error`, provider_name, provider_instance_id, active_turn_id, last_error,
**worker_id**, **pid**); `Turn` (state `running|interrupted|completed|error`, requested/started/
completed_at, assistant_message_id); `Message` (role `user|assistant|system`, text, attachments,
turn, streaming); `Activity` (tone `info|tool|approval|error`, kind, summary, payload, turn,
sequence); `ProposedPlan` (plan_markdown, implemented_at, implementation_thread); `Checkpoint`
(checkpoint_turn_count, checkpoint_ref, status `ready|missing|error`, files[], assistant_message);
`PendingRequest` (approval/user-input bookkeeping); `EventStreamCursor` (sequence allocation).

Enums: `RuntimeMode = approval-required|auto-accept-edits|auto|full-access`;
`InteractionMode = default|plan`. `ModelSelection = {instance_id, model, options}`.
`ChatAttachment`: images only — `type="image"`, id, name, mime_type (`image/*`), size_bytes ≤ 10 MiB.

The list view (`ThreadShell`) adds computed `latest_user_message_at`, `has_pending_approvals`,
`has_pending_user_input`, `has_actionable_proposed_plan`.

**One `type` namespace.** Store `RuntimeEvent`s in the same `tth_event` table and the same
per-thread sequence space as orchestration events, with `metadata.source in {"http","runtime"}`
distinguishing them. This merges two vocabularies (a fidelity loss vs T3) but is what makes a single
cursor, a single stream, and lossless reconnect simple. Two tables and two cursors would double the
SSE plumbing for little gain.

---

## 4. Runtime: backend seam, supervisor, pump, turn runner

Everything the HTTP layer needs from the runtime is one protocol. `InProcessBackend` implements it
by owning harnesses directly (this section); `SidecarBackend` implements it by JSON-RPC to the
harness worker that owns the thread (§10, M9). Routers and `dispatch()` talk only to this.

```python
# runtime/backends/base.py
class HarnessBackend(Protocol):
    async def ensure(self, thread: Thread) -> SessionInfo: ...
    async def start_turn(self, thread_id: str, prompt: SendTurnInput) -> str: ...   # -> turn_id
    async def interrupt(self, thread_id: str, turn_id: str | None) -> None: ...
    async def respond(self, thread_id, request_id, decision: ApprovalDecision) -> None: ...
    async def respond_user_input(self, thread_id, request_id, answers: Mapping) -> None: ...
    async def stop(self, thread_id: str, *, reason: str) -> None: ...
    async def shutdown(self) -> None: ...
```
Deliberately the same shape as `Harness` minus the streaming methods — events never return through
this interface, they go to the DB and out via `EventBroadcast` (§6). That is what lets the sidecar
RPC connection stay stateless enough to drop and reopen.

```python
# runtime/supervisor.py — the InProcessBackend's internals
@dataclass
class HarnessEntry:
    thread_id: str; harness: Harness
    stream: AsyncIterator[RuntimeEvent]        # from stream_events(), ALREADY subscribed
    pump_task: asyncio.Task[None]
    turn_task: asyncio.Task[None] | None
    turn_started: asyncio.Future[str] | None
    loop: asyncio.AbstractEventLoop; worker_id: str
    last_activity: float; session: Session | None

class HarnessSupervisor:      # == InProcessBackend
    @classmethod
    def instance(cls) -> HarnessSupervisor: ...
    async def ensure(self, thread: Thread) -> HarnessEntry: ...
    async def start_turn(self, thread_id: str, prompt: SendTurnInput) -> str: ...   # -> turn_id
    async def interrupt(self, thread_id: str, turn_id: str | None) -> None: ...
    async def respond(self, thread_id, request_id, decision: ApprovalDecision) -> None: ...
    async def respond_user_input(self, thread_id, request_id, answers: Mapping) -> None: ...
    async def stop(self, thread_id: str, *, reason: str) -> None: ...
    async def shutdown(self) -> None: ...
```

**`ensure()` ordering is mandatory** (finding (a)):
```python
async with self._lock_for(thread_id):
    if (e := self._entries.get(thread_id)) is not None:
        self._assert_same_loop(e); e.last_activity = now(); return e
    await self._claim_ownership(thread_id)       # DB CAS, below
    h = create_harness(thread.provider, **build_kwargs(thread))
    stream = h.stream_events()                   # eager subscribe after M0 fix
    pump = asyncio.create_task(pump_events(thread_id, stream), name=f"tth-pump:{thread_id}")
    session = await h.start_session(SessionStartInput(model=..., resume=...))
```

**Ownership CAS** turns the single-worker constraint from silent corruption into an explicit,
testable error:
```python
ThreadSession.objects.filter(thread_id=tid).filter(
    Q(worker_id=None) | Q(worker_id=me) | Q(status="stopped")
).update(worker_id=me, pid=os.getpid(), status="starting")
```
Zero rows updated ⇒ `ThreadOwnedByAnotherWorker` ⇒ **409 + `Retry-After`**. Plus
`if entry.loop is not asyncio.get_running_loop(): raise WrongEventLoop` — `respond()` resolves an
`asyncio.Future`, and cross-loop `set_result` is undefined behaviour.

**Turn runner** — the request never owns the generator (finding (b)):
```python
# runtime/turn.py
async def run_turn(entry: HarnessEntry, prompt: SendTurnInput) -> None:
    agen = entry.harness.send_turn(prompt)
    try:
        async for ev in agen:                    # discard — the pump persists
            if ev.type == "turn.started" and not entry.turn_started.done():
                entry.turn_started.set_result(ev.turn_id or "")
    except Exception as exc:
        if not entry.turn_started.done():
            entry.turn_started.set_exception(exc)
        await record_runtime_failure(entry.thread_id, exc)
    finally:
        await agen.aclose(); entry.turn_task = None
```
The view creates the task, then `await asyncio.wait_for(entry.turn_started,
TURN_START_TIMEOUT_SECONDS)` and returns `202 {turn_id, sequence}`. A disconnect after the 202
cancels nothing.

*Rejected:* persisting from `send_turn`'s stream — it double-writes (every yielded event is also on
the bus), interleaves two writers into one sequence space, and leaves session-lifetime events
(`session.exited`, out-of-turn `runtime.warning`) unhandled.

**Pump** — the sole DB writer:
```python
async def pump_events(thread_id: str, stream: AsyncIterator[RuntimeEvent]) -> None:
    async for batch in batched(stream, EVENT_BATCH_MAX, EVENT_BATCH_INTERVAL_MS):
        max_seq = await persist_batch(thread_id, batch)     # ONE sync_to_async hop
        await broadcast.publish(thread_id, max_seq)         # AFTER commit
```
The pump runs wherever the harness lives — in the web process under `InProcessBackend`, in
`tth_worker` under `SidecarBackend`. It is the sole DB writer either way, so the write path in §5 is
unchanged by topology; only its process moves.
`batched` takes one blocking `anext`, then drains with `asyncio.wait_for(anext, interval)` up to
`EVENT_BATCH_MAX`. Publishing after commit is what makes `after=<sequence>` lossless: anything an
SSE subscriber can miss is already durable.

**Lifecycle.** Lazy start on first `thread.turn.start` (or explicit `POST /threads/{id}/session`).
One reaper task (30 s tick) closes entries idle past `IDLE_TIMEOUT_SECONDS` with no `turn_task` and
no open `PendingRequest`. Shutdown via an ASGI lifespan wrapper the host installs —
`application = tth_lifespan(get_asgi_application())` — which cancels turn tasks, `aclose()`es
harnesses, drains pumps, marks sessions stopped, releases `worker_id`. On startup it runs a
**crash-recovery sweep**: stale `ThreadSession` rows bearing our `worker_id` → `stopped`; open
`PendingRequest` rows → `resolved_at=now, decision="abandoned"` (their futures died with the process).

---

## 5. Async / DB reality

**One `sync_to_async` per unit of work, not per query.** `transaction.atomic` cannot wrap awaits, so
every write path is a plain sync function wrapped once:

```python
_persist = sync_to_async(_persist_batch_sync, thread_sensitive=True)

def _persist_batch_sync(thread_id: str, events: Sequence[RuntimeEvent]) -> list[Envelope]:
    with transaction.atomic():
        start = allocate_sequences("thread", thread_id, len(events))
        rows = [OrchestrationEvent(...) for i, ev in enumerate(events)]
        OrchestrationEvent.objects.bulk_create(rows)
        projections.apply_many(thread_id, rows)      # read model, same txn
    return [Envelope.from_row(r) for r in rows]
```
`thread_sensitive=True` funnels ORM work onto asgiref's shared executor thread, which also
serialises sequence allocation in-process. If the pump contends with request-path ORM work, switch
it to a dedicated `ThreadPoolExecutor(max_workers=1)` — same serialisation, separate lane.

**Two sequence columns, deliberately.** `id = BigAutoField` for global insertion order (admin, GC);
`sequence` per-aggregate monotonic, the only thing the SSE cursor uses (streams are per-thread).
```python
def allocate_sequences(kind: str, agg_id: str, n: int) -> int:
    cur, _ = EventStreamCursor.objects.select_for_update().get_or_create(
        aggregate_kind=kind, aggregate_id=agg_id, defaults={"last_sequence": 0})
    start = cur.last_sequence + 1
    cur.last_sequence += n; cur.save(update_fields=["last_sequence"])
    return start
```
Guarded by `UniqueConstraint(fields=["aggregate_kind","aggregate_id","sequence"])` so a gap or dup
is a hard error, not silent stream corruption. A native Postgres `SEQUENCE` is rejected: not
per-aggregate, doesn't port to SQLite.

**SQLite vs Postgres.** `select_for_update()` is a no-op on SQLite, but SQLite serialises writers at
the file level — correctness holds *provided* writes are in `atomic` and the connection takes the
write lock up front:
```python
"OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE",   # Django 5.1+
            "init_command": "PRAGMA journal_mode=WAL;"}
```
Without `IMMEDIATE` + WAL, a concurrent SSE reader plus the pump writer yields `database is locked`.
Postgres is the production recommendation; SQLite is fine single-user/dev.

**SSE must not pin a connection.** Replay is a sequence of independent `sync_to_async` pages
(`SSE_REPLAY_PAGE_SIZE`, keyed on `sequence > last_sent`), never a queryset iterator held across
awaits; each page calls `close_old_connections()`. Recommend `CONN_MAX_AGE = 0` on the SSE process.

---

## 6. HTTP surface

**REST-shaped resource routes as the primary surface**, each a thin wrapper over one internal
`dispatch(command, ctx)`; plus `POST /commands` as an escape hatch. django-ninja's payoff is
per-route pydantic bodies, a real OpenAPI document, and per-route `auth=`/throttling — a single
`POST /commands` with a 22-member union collapses OpenAPI to one opaque blob and makes per-action
permissions awkward. T3's command/event core stays internal, where the fidelity actually matters.

```
GET    /harnesses                                  -> [{name, registered, capabilities}]
GET    /healthz                                    -> {worker_id, pid, live_harnesses, queue_depth}

POST   /projects   GET /projects   GET|PATCH|DELETE /projects/{id}

POST   /threads    GET /threads?project_id=&archived=&pinned=&cursor=   -> ThreadShell page
GET    /threads/{id}   PATCH /threads/{id}   DELETE /threads/{id}
POST   /threads/{id}/{archive|unarchive|settle|unsettle|snooze|unsnooze|pin|unpin}
PUT    /threads/{id}/runtime-mode      PUT /threads/{id}/interaction-mode
GET    /threads/{id}/{messages|activities|plans|checkpoints}?after=&limit=

POST   /threads/{id}/turns                         -> 202 {turn_id, sequence}
POST   /threads/{id}/turns/{turn_id}/interrupt     -> 202
POST   /threads/{id}/session/stop                  -> 202
POST   /threads/{id}/checkpoints/{turn_id}/revert  -> 202 (501 until M8)

POST   /threads/{id}/approvals/{request_id}   {decision}   -> 204
POST   /threads/{id}/user-input/{request_id}  {answers}    -> 204

GET    /threads/{id}/events?after=<seq>&types=&live=1      -> text/event-stream
GET    /threads/{id}/events/history?after=&limit=          -> JSON page (same rows)
POST   /commands   {command:{type:...}, command_id?, correlation_id?}
```

**SSE semantics** (`routers/events_sse.py`) — the ordering is what makes reconnect lossless:
```python
@router.get("/threads/{thread_id}/events", response=None)
async def stream_events(request, thread_id: str, after: int = 0, live: bool = True):
    await authorize_thread(request, thread_id, "view")
    if (lei := request.headers.get("Last-Event-ID")) and not after:
        after = int(lei)
    sub = hub.subscribe(thread_id, maxsize=SSE_BACKLOG_LIMIT)   # BEFORE the replay read
                                                                # hub is fed by EventBroadcast (§10)
    return StreamingHttpResponse(_generate(sub, thread_id, after, live),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```
`_generate`: (1) subscribe to the hub **first**, so nothing published during replay is missed;
(2) `yield "retry: 3000\n\n"`; (3) replay pages where `sequence > after`, tracking `last_sent`;
(4) switch to live, **discarding envelopes with `sequence <= last_sent`** (the overlap from step 1);
(5) loop on `asyncio.wait_for(sub.get(), SSE_KEEPALIVE_SECONDS)`, emitting `: keepalive\n\n` on
timeout; (6) `finally: hub.unsubscribe(sub)`.

Frame format — `id:` is load-bearing, it makes a browser `EventSource` resend `Last-Event-ID` on
auto-reconnect so the lossless path needs zero client code:
```
id: 4711
event: content.delta
data: {"type":"content.delta","text":"Hel",...}
```

**Backpressure.** Hub subscriber queues are bounded. On overflow the hub emits
`event: overflow\ndata:{"after":<last_seq>}` and drops the subscriber; the client reconnects with
that cursor and the DB fills the gap. A slow SSE client can therefore never stall the pump — which
matters because `EventBus` is unbounded with no backpressure of its own.

**Disconnect detection.** Django has no `request.is_disconnected()`; the ASGI handler closes the
async iterator, so cleanup lives in the generator's `finally` (`GeneratorExit`/`CancelledError`).
The keepalive write is what actually surfaces a dead peer. Document: `GZipMiddleware` must not apply
to `text/event-stream`, and nginx needs `proxy_buffering off` (or the `X-Accel-Buffering` header).

---

## 7. Auth and security posture

`TALKTOHARNESSES["AUTH"]` is a dotted path resolved at API construction to anything django-ninja
accepts. Ship `django_session_auth` (default), `ApiKeyHeaderAuth` (hashed keys from settings), and
`allow_any` for dev — with a `django.core.checks` **error** if `allow_any` is active while
`DEBUG=False`.

Two pluggable per-object hooks, both dotted paths:
`can(actor, action: str, obj) -> bool` — called by `dispatch()` before every command and by every
mutating route; `filter_threads(actor, qs) -> QuerySet` — gates list endpoints **and the SSE
subscribe** (an unauthorised thread stream is the easiest thing to forget). Defaults: superuser or
`obj.created_by == actor`, backed by Django model perms plus custom `Meta.permissions`
(`start_turn`, `respond_approval`, `use_elevated_runtime_mode`) so the standard admin works.

**State this in the README in these words:** *This app executes arbitrary local programs (`codex`,
`claude`, `cursor-agent`, …) in an operator-chosen directory, driven by user-supplied prompts. In
`auto-accept-edits` / `auto` / `full-access` runtime modes the agent's shell commands and file
writes are auto-approved. Anyone who can start a turn can run code as the server's OS user. Treat
every authenticated user as having a shell.*

Controls, all v1:
1. `ALLOWED_WORKSPACE_ROOTS` must be non-empty (empty ⇒ thread creation 403s) unless
   `ALLOW_ANY_WORKSPACE=True`. `runtime/workspace.py::resolve_workspace(raw) -> Path` does
   `Path(raw).resolve()` **first** (defeats `..` and symlinks), then requires
   `resolved.is_relative_to(root.resolve())` for some root, then `is_dir()`. Else 403.
2. **Client-settable harness kwargs are allowlisted to `{model}`.** `cwd` goes through (1).
   `binary`, `command`, `env`, `codex_home`, `client_factory`, `base_url` come **only** from
   `HARNESS_KWARGS` in settings. `command=` is the test seam — exposing it over HTTP is unmediated
   RCE. `extra="forbid"` on the request schema is the enforcement.
3. Runtime modes above `approval-required` require both `ALLOW_ELEVATED_RUNTIME_MODES` and the
   `use_elevated_runtime_mode` permission.
4. `MAX_CONCURRENT_HARNESSES`, a per-user active-thread cap, ninja throttling on `POST /turns`.
5. Attachments: images only, `mime_type` must start `image/`, ≤ 10 MiB, validate magic bytes rather
   than trusting the declared type.
6. `ProcessError.stderr` tails can contain tokens and absolute paths — surface only when `DEBUG` or
   `actor.is_staff`.

---

## 8. Error mapping

```python
# errors.py — ordered most-specific first; walked by isinstance
STATUS_BY_EXC = (
    (WorkspaceNotAllowed,        403, "workspace_not_allowed"),
    (ThreadOwnedByAnotherWorker, 409, "thread_owned_elsewhere"),   # + Retry-After
    (ConcurrentTurn,             409, "turn_already_running"),
    (UnknownHarnessError,        404, "unknown_harness"),
    (MissingDependencyError,     501, "missing_dependency"),
    (ApprovalError,              409, "request_not_open"),
    (SessionError,               409, "session_state"),
    (tth_errors.TimeoutError,    504, "timeout"),
    (ProcessError,               502, "process_failed"),           # before TransportError
    (TransportError,             502, "transport_failed"),
    (ProtocolError,              502, "protocol_error"),
    (HarnessRuntimeError,        502, "provider_error"),
    (TalkToHarnessesError,       500, "internal"),
)
```
Body: `{"error": {"type","code","message","detail"}}`. django-ninja's `ValidationError` keeps its
default 422. 5xx paths `logger.exception(...)` with `thread_id`/`turn_id` in `extra`.

`talktoharnesses.errors.TimeoutError` shadows the builtin — import it qualified
(`from talktoharnesses import errors as tth_errors`) so the table doesn't catch
`builtins.TimeoutError` from `asyncio.wait_for`, which maps to 504 under a different code
(`turn_start_timeout`).

---

## 9. Testing

`pytest-django` in the dev group; tests under `tests/django/` with their own `conftest.py`.
`DJANGO_SETTINGS_MODULE = "tests.django.settings"` and `django_find_project = false` in
`[tool.pytest.ini_options]`. Existing library tests are unaffected (pytest-django is inert without
`django_db`). Guard the new directory with `pytest.importorskip("ninja")` so `--no-dev` still runs
the library suite.

**Two traps, each worth its own regression test:**
1. **In-memory SQLite + async.** `sync_to_async(thread_sensitive=True)` runs ORM work on asgiref's
   shared executor thread — a *different* connection. Under plain `@pytest.mark.django_db` (a
   never-committed atomic block) that thread sees an empty DB. Use
   `@pytest.mark.django_db(transaction=True)` for every async/HTTP test, and a **file-backed** temp
   SQLite with WAL, not `:memory:`.
2. **Async client.** `django.test.AsyncClient` exposes `streaming_content` as an async iterator —
   enough for SSE assertions. Add one uvicorn-backed smoke test too; middleware/buffering problems
   only appear over a real socket.

**Reuse the existing mock peers** — no agent CLIs, driven through `HARNESS_KWARGS`:
```python
FIXTURES = Path(__file__).parents[1] / "fixtures"
settings.TALKTOHARNESSES = {
    "ALLOWED_WORKSPACE_ROOTS": [tmp_path],
    "AUTH": "talktoharnesses_django.auth.allow_any",
    "HARNESS_KWARGS": {
        "codex":    {"command": [sys.executable, str(FIXTURES / "codex_mock_peer.py")]},
        "cursor":   {"command": [sys.executable, str(FIXTURES / "acp_mock_agent.py")]},
        "grok":     {"command": [sys.executable, str(FIXTURES / "acp_mock_agent.py")]},
        "opencode": {"command": [sys.executable, str(FIXTURES / "opencode_mock_server.py")]},
        "claude":   {"client_factory": "tests.fixtures.claude_fake_client:FakeClaudeClient"},
    },
}
```
`HARNESS_KWARGS` values accept raw callables and `"dotted.path:name"` strings. The claude fake lives
inline in `tests/test_conformance.py` today — extract it to `tests/fixtures/claude_fake_client.py`
in M0 so both suites share it.

**HTTP conformance test**, mirroring `tests/test_conformance.py`:
```python
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("provider", ["claude", "codex", "cursor", "grok", "opencode"])
async def test_http_turn_conformance(provider, aclient, tmp_path, mock_harnesses):
    project = await post(aclient, "/projects", {...})
    thread  = await post(aclient, "/threads", {"project_id": ..., "provider": provider, ...})
    async with sse(aclient, f"/threads/{thread['id']}/events?after=0") as stream:
        turn = await post(aclient, f"/threads/{thread['id']}/turns", {"prompt": "reply with OK"})
        frames = await collect_until(stream, lambda f: f.event == "turn.completed", timeout=30)
    assert "content.delta" in [f.event for f in frames]
    assert "OK" in "".join(f.data["text"] for f in frames
                           if f.event == "content.delta" and f.data["content_kind"] == "text")
    assert all(f.data["provider"] == provider for f in frames)
    seqs = [f.id for f in frames]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))                     # dense, ordered
    assert frames[0].event in {"session.started", "thread.started", "turn.started"}   # M0 fix
    detail = await get(aclient, f"/threads/{thread['id']}")
    assert detail["latest_turn"]["state"] == "completed"
    assert "OK" in detail["messages"][-1]["text"]
```
Helper `tests/django/sse_utils.py::parse_sse_frames(chunks) -> AsyncIterator[Frame(id, event, data)]`.
The library's `transports/sse.py::aiter_sse_json` is **not** reusable — it discards `event:` and
`id:`, and we assert on both.

Also: `test_sequence_allocation.py` (200 concurrent appends, two aggregates — dense, gap-free, no
dupes); `test_reconnect_lossless.py` (drop mid-stream, reconnect with `after=`, and the
`Last-Event-ID` path — assert the concatenation equals the DB log exactly once);
`test_disconnect_does_not_kill_turn.py` (finding (b) regression — disconnect the SSE client mid-turn,
assert `turn.completed` still lands in the DB); `test_approvals.py` (opencode mock with
`TALKTOHARNESSES_OPENCODE_APPROVAL=1` — `request.opened` over SSE, 204, `request.resolved`, second
POST 409); `test_projections.py` (canned `RuntimeEvent` lists straight into `apply_many` — no
subprocess, fast, and where most read-model bugs live); `test_wrong_worker.py` (409 + `Retry-After`);
`test_workspace_allowlist.py` (traversal, symlink escape, non-dir, empty allowlist);
`test_command_kwargs_rejected.py` (the RCE regression — `command`/`binary`/`env` in the body rejected).

**Topology tests (M8/M9), all in-process — no real multi-process deployment needed:**
- `test_broadcast_backends.py` — parametrized over `inprocess`/`polling`/`postgres`/`redis`
  (`skipif` on service availability), asserting the same `publish`/`subscribe` contract, and that a
  *dropped* notification costs only latency because the next one carries a higher `max_sequence`.
- `test_sse_foreign_owner.py` — write events directly through the pump's persist path, subscribe SSE
  from a hub that never saw them in memory. This is the M8 property without spawning a second
  process.
- `test_sidecar_backend.py` — run `tth_worker`'s server coroutine in the test's own loop over
  `asyncio.open_unix_connection` to a `tmp_path` socket, drive it through `SidecarBackend`, and
  re-run the **5-provider conformance test unchanged against it**. Same assertions, different
  backend — that equivalence is the whole point of the seam, so it should be a parametrized fixture
  over `HARNESS_BACKEND` rather than a separate test.
- `test_worker_routing.py` — claim caps (`WORKER_MAX_THREADS`), least-loaded selection, stale
  heartbeat sweep, drain refusing new claims while finishing in-flight turns, and 503 + `Retry-After`
  when no worker is reachable.

---

## 10. Deployment topology

### Why `uvicorn --workers N` is the failure mode, not the fix

`--workers N` forks N processes sharing one listening socket; the kernel distributes accepts
arbitrarily. Each has its own event loop, its own memory, its own `dict[thread_id, Harness]`. There
is no affinity and no routing control. With the in-process backend, roughly (N−1)/N of follow-up
requests land on a process that has never heard of the thread:

| Request | Lands on | Result |
|---|---|---|
| `POST /turns` | A | harness spawned in A; futures live in A |
| `POST /approvals/{id}` | C | C's `_pending_approvals` is empty — the agent blocks until timeout |
| `GET /events` | B | replays from DB, then hangs on keepalives; live fan-out is A's in-memory hub |
| `POST /interrupt` | C | no entry — 409 |

The ownership CAS (§4) converts most of this from silent breakage into `409 + Retry-After`. That is
a diagnostic, not a scaling story.

### The two coupled problems, separated

**Read path (SSE fan-out) is easy.** The event log plus the `after=<sequence>` cursor already make
any thread's stream reconstructible by any process. Replace the in-memory hub with a broadcast
backend and any web worker can serve any thread. → **M8**.

**Write path (approval futures) is hard.** `respond()` resolves an `asyncio.Future` held in the
driver's instance dict; that is irreducibly in-process and in-loop. No broker fixes it. Either route
to the owning process, or move the harness out of the web tier entirely. → **M9**.

### Three supported topologies

**T1 — Single process.** Dev, single-user, small teams.
`uvicorn --workers 1` · `HARNESS_BACKEND="inprocess"` · `BROADCAST_BACKEND="inprocess"` ·
SQLite (WAL) or Postgres. The web process only serializes JSON, batches DB writes, and fans out SSE
— the actual work is in the agent subprocesses. `MAX_CONCURRENT_HARNESSES` defaults to 8 because RAM
and provider rate limits bind long before the event loop does.

**T2 — Sticky-routed.** Interim; documented because people will try it, and discouraged.
N *separate* uvicorn processes on N ports (not `--workers`) behind nginx
`hash $thread_id consistent`, extracting the id from the path. It works, but a worker restart
orphans that worker's agent subprocesses and strands its pending approvals, and any change to the
worker set rebalances threads onto processes that don't own them. Use only if you need scale before
M9 lands.

**T3 — Split tier.** The target.
- N **web workers** — stateless; `--workers N` is now correct. Serve REST + SSE. Never spawn agents,
  never touch the workspace filesystem, don't need the agent CLIs installed.
- K **harness workers** — `manage.py tth_worker`. Each owns a claimed set of threads, runs their
  pumps, writes the event log, applies projections, broadcasts. These need the agent CLIs on `PATH`
  and the workspace mounted.
- Postgres + a broadcast backend.

The split is also a security boundary: the tier reachable from the network has no workspace access
and no ability to exec. That directly narrows the posture in §7 — "anyone who can start a turn can
run code as the server's OS user" becomes "…as the *harness worker's* OS user", which can be a
separate, unprivileged, containerized account.

### Routing and ownership

Reuse the CAS rather than adding consistent hashing. `ThreadSession.worker_id` +
`ThreadSession.worker_endpoint` *is* the routing table:

- **new thread** → least-loaded live worker (from `HarnessWorker` heartbeat rows, capped by
  `WORKER_MAX_THREADS`) claims it through the same CAS already built in M4;
- **existing thread** → the web worker reads `worker_id`, resolves the endpoint, opens or reuses a
  JSON-RPC connection;
- **dead worker** → heartbeat stale past `WORKER_HEARTBEAT_TIMEOUT`; the sweeper marks its sessions
  `stopped`, abandons its `PendingRequest` rows (their futures died with the process), and releases
  `worker_id` so the threads can be re-claimed;
- **graceful drain** → the worker stops accepting claims, finishes in-flight turns, exits; the web
  tier returns 503 + `Retry-After` for the draining window.

Claim-based assignment survives worker-set changes without rebalancing, and reuses machinery M4
already needs.

### Sidecar IPC — reuse `transports/stdio_jsonrpc.py`, don't invent a protocol

`JsonRpcPeer(reader, writer)` is already a complete bidirectional newline-delimited JSON-RPC 2.0
peer: `request(timeout=)`, `notify`, `respond`, `respond_error`, and pending-future failure on EOF.
Critically, it dispatches inbound requests on **separate tasks** — the code comments say this is
exactly so a blocked approval handler cannot stall the read loop. That is the property the sidecar
needs, already written and already tested against `tests/fixtures/echo_jsonrpc_peer.py`.

It takes any asyncio reader/writer pair, so `asyncio.open_unix_connection(path)` (same host) or
`asyncio.open_connection(host, port)` (multi-host, private network or mTLS) drops straight in. No
new protocol code, no new dependency.

```
session.ensure      {thread_id, provider, kwargs}      -> {session}
turn.start          {thread_id, prompt, model}         -> {turn_id}   # returns at turn.started
turn.interrupt      {thread_id, turn_id?}              -> {}
approval.respond    {thread_id, request_id, decision}  -> {}
user_input.respond  {thread_id, request_id, answers}   -> {}
session.stop        {thread_id, reason}                -> {}
harness.close       {thread_id}                        -> {}
worker.status       {}                                 -> {threads, load, uptime, version}
```

Runtime events do **not** cross this connection. The harness worker writes them to the DB and
broadcasts a notification. One write path, one sequence space, and the RPC connection carries no
stream state — so dropping and reopening it is free.

`resolve_workspace()` (§7) runs on **both** tiers: the web worker rejects early for a good error
message, the harness worker re-validates because it is the tier that actually execs.

### Broadcast backends

The notification carries `{thread_id, max_sequence}` only — never payloads. The subscriber then
reads rows `sequence > cursor` from the DB. Postgres `NOTIFY` caps at 8000 bytes, payload-carrying
would double-serialize, and notification-only makes replay and live use the *identical* code path.
The DB stays the single source of truth, so "lossless reconnect" needs no separate argument.

```python
# runtime/broadcast/base.py
class EventBroadcast(Protocol):
    async def publish(self, thread_id: str, max_sequence: int) -> None: ...
    def subscribe(self, thread_id: str) -> AsyncIterator[int]: ...
```
- `inprocess` — M3; T1 only.
- `polling` — universal fallback. `SELECT max(sequence) WHERE thread_id=? AND sequence > cursor`
  every `BROADCAST_POLL_MS` (default 250). Zero infra, works on SQLite, latency bounded by the
  interval. Default for T3 on SQLite.
- `postgres` — `LISTEN/NOTIFY` on a dedicated async psycopg3 connection. Django's ORM exposes no
  async LISTEN, so this connection is managed outside the ORM pool and must not be counted against
  `CONN_MAX_AGE`. Default for T3 on Postgres.
- `redis` — for multi-host deployments that would rather not hold a PG connection per web worker.

Note the hub's bounded-queue + overflow-and-resume behaviour (§6) is unchanged: on overflow the
subscriber is dropped with its cursor and reconnects, and the DB fills the gap. That mechanism is
what makes a lossy broadcast transport acceptable — a dropped NOTIFY costs latency, never data.

---

## 11. Milestones

| M | Scope | Ships as |
|---|---|---|
| **M0** | **Library prep.** Make `stream_events()` subscribe eagerly in all 4 implementations: `def stream_events(self): return self._bus.iter_queue(self._bus.subscribe())` — delete `_stream_events`; `iter_queue` already unsubscribes in its `finally`, so this is behaviour-preserving and strictly safer. Regression test emitting immediately after `stream_events()` returns. Extract the fake Claude client to `tests/fixtures/claude_fake_client.py`. | A library bugfix, useful standalone |
| **M1** | Packaging + skeleton: pyproject, `apps.py`, `conf.py` + `setting_changed`, `checks.py`, `errors.py` handlers, `api.py`/`urls.py`, `GET /harnesses`, `GET /healthz`, `tests/django/settings.py` | A mountable app reporting available harnesses |
| **M2a** | Models + `0001_initial` + `EventStreamCursor` + `allocate_sequences` + `domain/events.py` + `dispatch()` + non-runtime handlers (`project.*`, `thread.create/delete/archive/settle/snooze/pin/meta/modes`) | Event-sourced core, ORM-testable |
| **M2b** | Projections + read-model routes, `POST /commands`, `GET /events/history`, `admin.py` | Usable orchestration API, no agents attached |
| **M3** | `runtime/hub.py`, `sse.py`, `GET /threads/{id}/events` replay→live, keepalive, `Last-Event-ID`, bounded queues + overflow-and-resume. **Introduces the `EventBroadcast` seam** with the `inprocess` impl. Tested by injecting events through `dispatch()` | Live streaming, still no subprocesses |
| **M4** | **Introduces the `HarnessBackend` seam** + `InProcessBackend`: `supervisor.py`, `pump.py`, `turn.py`, `workspace.py`, `lifespan.py`, ownership CAS, idle reaper, crash-recovery sweep; `thread.turn.start/interrupt`, `session.stop/set`, assistant-message projections. **5-provider HTTP conformance test lands here.** | **T1** — the working product |
| **M5** | Approvals + user input: routes, `PendingRequest`, `has_pending_*`, 409 on stale ids, abandon-on-teardown | Interactive agents |
| **M6** | Plans, diffs, activities, checkpoints: `proposed-plan.upsert`, `turn.diff.complete`, `activity.append`, `Checkpoint` (status `missing` with no VCS layer), `has_actionable_proposed_plan` | Full T3 read-model parity |
| **M7** | Auth hardening (permission hooks, elevated-mode perm, throttling), `tth_gc_events`, `tth_status`, `docs/django/README.md` + T1/T2 deployment guide | Production-ready, single-process |
| **M8** | **Multi-worker read path.** `broadcast/{polling,postgres,redis}.py`; hub fed by `EventBroadcast`; SSE correct from any web worker regardless of which process owns the harness. Verified with N web workers against a T1 harness owner | N-worker web tier for reads |
| **M9** | **Harness worker + sidecar backend (T3).** `management/commands/tth_worker.py`; `runtime/worker/{server,routing,heartbeat,drain}.py`; `backends/sidecar.py` over `JsonRpcPeer` on unix/tcp; `HarnessWorker` heartbeat model + `ThreadSession.worker_endpoint`; claim-based assignment, stale-worker sweep, graceful drain; workspace re-validation on the worker; `worker.status` / worker `/healthz` | **T3** — full horizontal scale |
| **M10** | Deferred remainder: `thread.checkpoint.revert` (needs a git/worktree layer) and title regeneration — both orthogonal to topology | v2 features |

---

## 12. Verification

Gates, in order:
```bash
uv run pytest                       # library suite — must stay green (M0 touches drivers)
uv run pytest tests/django          # new suite
uv run pytest -m live               # opt-in, real CLIs
uv run mypy --strict src/           # now covers talktoharnesses_django too
uv run ruff check
```

End-to-end smoke against a real agent, once M4 lands:
```bash
uv run python -m tests.django.manage migrate
uv run uvicorn tests.django.asgi:application --workers 1 --port 8000
curl -s localhost:8000/api/harnesses | jq
PID=$(curl -sX POST localhost:8000/api/projects -d '{"name":"p","workspace_root":"'$PWD'"}' | jq -r .id)
TID=$(curl -sX POST localhost:8000/api/threads -d '{"project_id":"'$PID'","provider":"codex"}' | jq -r .id)
curl -N "localhost:8000/api/threads/$TID/events?after=0" &     # attach first
curl -sX POST "localhost:8000/api/threads/$TID/turns" -d '{"prompt":"list the files here"}'
```
Confirm on the SSE stream: `turn.started` → `content.delta`* → `turn.completed`, monotonic `id:`,
`: keepalive` during idle. Then kill the `curl -N`, reconnect with `after=<last id>`, and confirm
the turn completed regardless and the replay has no gap or duplicate. Finally
`curl localhost:8000/api/threads/$TID | jq .messages` and check the assistant text matches the
concatenated deltas, and `django-admin` shows the event log.

**Topology gates.**

T1 (M4): `--workers 2` with `HARNESS_BACKEND="inprocess"` must produce a 409 with `Retry-After` on
the second worker — an explicit refusal, never corruption.

T3-read (M8): keep one harness owner, run 4 web workers, and attach the SSE stream through a
round-robin proxy. Every worker must serve the full stream — replay plus live — for a thread it does
not own. Kill and reattach mid-turn against a *different* worker each time; the concatenation must
still equal the DB log exactly once, no gaps, no duplicates.

T3-full (M9):
```bash
manage.py tth_worker --listen unix:///run/tth/w1.sock &   # needs agent CLIs + workspace
manage.py tth_worker --listen unix:///run/tth/w2.sock &
uvicorn ...asgi:application --workers 4                   # no CLIs, no workspace mount
```
Assert: turns start on either harness worker; approvals posted to *any* web worker reach the owning
harness worker and unblock the agent; `SIGTERM` to w1 drains in-flight turns and then releases its
threads for re-claim by w2; `kill -9` to w1 leaves the sweeper to mark sessions `stopped` and
abandon its `PendingRequest` rows within `WORKER_HEARTBEAT_TIMEOUT`; and a web worker with no
reachable harness worker returns 503 + `Retry-After` rather than hanging.

## 13. Risks

1. **Loop-bound approval futures are the root constraint.** `_pending_approvals` lives in one
   process on one loop. Through M7 this is contained by the DB CAS → 409, the `entry.loop` identity
   check, `/healthz` exposing `backend`/`worker_id`, and T1 in the docs. M8 removes it from the read
   path, M9 from the write path. Residual risk after M9: a harness worker crash still strands its
   in-flight approvals — the sweep abandons them cleanly, but those turns are lost, not resumed.
2. **`send_turn()` cancellation** (finding (b)) — if the turn runner is ever "simplified" into the
   request handler, turns die on client disconnect for claude/cursor/grok and nowhere else. Comment
   citing `drivers/claude.py:219`, plus the regression test.
3. **Write amplification from `content.delta`** — a long answer is thousands of rows. Batch inserts,
   coalesce delta text into the `Message` row once per batch (not per delta), offer
   `PERSIST_RAW=False` (`raw` roughly doubles row size), ship the retention GC in M7.
4. **Unbounded `EventBus`** (`_event_bus.py:23`) — if the pump stalls on the DB, memory grows without
   bound. Keep the persist path free of network I/O, expose queue depth on `/healthz`, warn above a
   threshold.
5. **SQLite locking** — WAL + `IMMEDIATE` + `timeout` are not optional; Postgres in prod.
6. **django-ninja exception handlers are per-`NinjaAPI`** — the bare-router mount silently loses
   error mapping. Consider documenting the ready-made `NinjaAPI` as the only supported path.
7. **mypy `--strict` + django-stubs** is a known friction source; budget time, scope a per-module
   relaxation for `migrations.*` if needed.
8. **`StreamingHttpResponse` under WSGI** degrades silently — add a `django.core.checks` warning when
   `WSGI_APPLICATION` is set and `ASGI_APPLICATION` is not.
9. **Swappable user model** — every user FK must use `settings.AUTH_USER_MODEL` with
   `migrations.swappable_dependency`, or the app breaks in half of all host projects.
