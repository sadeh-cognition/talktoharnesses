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

1. **In-process, ASGI-only.** The app owns harnesses in an in-memory registry on the server's event
   loop. Single worker (or `thread_id`-sticky routing) is a documented requirement.
2. **SSE via django-ninja.** Async endpoints returning `StreamingHttpResponse`. No django-channels.
3. **Full event log + read model.** Append-only event table projected into Django models — replay,
   reconnect-with-cursor, history APIs, Django admin.
4. **Scope includes thread/project orchestration**, not just raw harness control.

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
  routers/     meta.py  projects.py  threads.py  turns.py  requests.py
               events_sse.py  commands.py
  sse.py  admin.py  migrations/0001_initial.py
  management/commands/tth_gc_events.py  tth_status.py
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

## 4. Runtime: supervisor, pump, turn runner

```python
# runtime/supervisor.py
@dataclass
class HarnessEntry:
    thread_id: str; harness: Harness
    stream: AsyncIterator[RuntimeEvent]        # from stream_events(), ALREADY subscribed
    pump_task: asyncio.Task[None]
    turn_task: asyncio.Task[None] | None
    turn_started: asyncio.Future[str] | None
    loop: asyncio.AbstractEventLoop; worker_id: str
    last_activity: float; session: Session | None

class HarnessSupervisor:
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
        envelopes = await persist_batch(thread_id, batch)   # ONE sync_to_async hop
        hub.publish(thread_id, envelopes)                   # AFTER commit
```
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

---

## 10. Milestones

| M | Scope | Ships as |
|---|---|---|
| **M0** | **Library prep.** Make `stream_events()` subscribe eagerly in all 4 implementations: `def stream_events(self): return self._bus.iter_queue(self._bus.subscribe())` — delete `_stream_events`; `iter_queue` already unsubscribes in its `finally`, so this is behaviour-preserving and strictly safer. Regression test emitting immediately after `stream_events()` returns. Extract the fake Claude client to `tests/fixtures/claude_fake_client.py`. | A library bugfix, useful standalone |
| **M1** | Packaging + skeleton: pyproject, `apps.py`, `conf.py` + `setting_changed`, `checks.py`, `errors.py` handlers, `api.py`/`urls.py`, `GET /harnesses`, `GET /healthz`, `tests/django/settings.py` | A mountable app reporting available harnesses |
| **M2a** | Models + `0001_initial` + `EventStreamCursor` + `allocate_sequences` + `domain/events.py` + `dispatch()` + non-runtime handlers (`project.*`, `thread.create/delete/archive/settle/snooze/pin/meta/modes`) | Event-sourced core, ORM-testable |
| **M2b** | Projections + read-model routes, `POST /commands`, `GET /events/history`, `admin.py` | Usable orchestration API, no agents attached |
| **M3** | `runtime/hub.py`, `sse.py`, `GET /threads/{id}/events` replay→live, keepalive, `Last-Event-ID`, bounded queues + overflow-and-resume. Tested by injecting events through `dispatch()` | Live streaming, still no subprocesses |
| **M4** | `supervisor.py`, `pump.py`, `turn.py`, `workspace.py`, `lifespan.py`, ownership CAS, idle reaper, crash-recovery sweep; `thread.turn.start/interrupt`, `session.stop/set`, assistant-message projections. **5-provider HTTP conformance test lands here.** | The actual product |
| **M5** | Approvals + user input: routes, `PendingRequest`, `has_pending_*`, 409 on stale ids, abandon-on-teardown | Interactive agents |
| **M6** | Plans, diffs, activities, checkpoints: `proposed-plan.upsert`, `turn.diff.complete`, `activity.append`, `Checkpoint` (status `missing` with no VCS layer), `has_actionable_proposed_plan` | Full T3 read-model parity |
| **M7** | Auth hardening (permission hooks, elevated-mode perm, throttling), `tth_gc_events`, `tth_status`, `docs/django/README.md` + deployment guide | Production-ready |
| **M8** | Deferred: `thread.checkpoint.revert` (needs a git/worktree layer), title regeneration, and the real fix for single-worker — a per-thread harness **sidecar process** where the futures live, fronted by a small IPC protocol, letting the Django tier scale to N workers | v2 |

---

## 11. Verification

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

Deployment sanity: `--workers 2` must produce a 409 with `Retry-After` on the second worker, not
corruption.

## 12. Risks

1. **Single-worker is the load-bearing constraint.** `_pending_approvals` futures live in one
   process on one loop. Mitigated by the DB CAS → 409, the `entry.loop` identity check, `/healthz`
   exposing `worker_id`, and docs mandating `--workers 1` or `thread_id`-sticky routing. The only
   real fix is M8's sidecar.
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
