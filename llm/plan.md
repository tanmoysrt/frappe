# Frappe Async Migration — Execution Plan

Derived from [research.md](research.md). Converts Frappe from sync/WSGI to async-native/ASGI in small, shippable phases.

**Ground rules — every phase:**

1. Ship behind the existing API — no breaking change for apps.
2. All tests green before the phase ships.
3. Previous behavior one config flip away (rollback). Sole exception: Phase 18 (deletion), which runs last.
4. The framework is fully usable after every phase.

---

## 1. Architecture

### 1.1 Light Mode = Default: One Process, One Loop

This plan builds **light mode** — the default deployment: a **single process, single asyncio event loop**, started with just `python3 app.py` — uvicorn run programmatically (`uvicorn.Server(config).serve()` awaited inside the loop), never the uvicorn CLI, never `--workers`. Out of the box, the simplest thing that works.

```
python3 app.py
  └── one asyncio loop
        ├── uvicorn server (HTTP/ASGI)
        ├── task queue workers (3–4 asyncio tasks)
        ├── scheduler tick task
        └── socket.io server
```

- **No multiprocess in light mode.** Everything shares memory: in-process caches, direct `sio.emit()`, queue state, scheduler — no IPC, no pickling, no Redis pub/sub.
- Sync code runs in a **sized thread pool** inside the same process (§1.3).

### 1.2 Large Deployments: Config, Not a Fork

Large deployments are not light mode and stay fully supported via config: RQ + separate worker processes, multi-process serving, `AsyncRedisManager` for realtime. Nothing the big-bench path depends on gets removed — light vs heavy is a config choice, permanently.

### 1.3 Concurrency Model: Thread Pool, NOT `thread_sensitive=True`

**Hard-won lesson from this bench (2026-06-05):** asgiref's `thread_sensitive=True` runs ALL sync code in one shared thread — every sync handler serialized, concurrency = 1, measured at **16 rps** vs ~119 rps with real concurrency. Do not repeat this.

- All `sync_to_async` calls use `thread_sensitive=False` with a sized `ThreadPoolExecutor` (~2×CPU workers) set as the loop's default executor.
- Safe because all per-request state lives in `frappe.local` (ContextVar-backed) — asgiref copies the context into the worker thread and propagates writes back. No sync code shares thread-local state across requests.
- Applies everywhere: HTTP handler dispatch, queue job execution, hooks. A `thread_sensitive=True` anywhere reintroduces the serial bottleneck silently.
- Phase 0 includes a spike test proving `frappe.local` propagation across both bridge directions before anything ships.

---

## 2. Compat Strategy: Async-Native, Sync Fallback (asgiref)

Async is the default API. Sync keeps working — purely via asgiref, no custom event-loop juggling — because custom apps need time to port. Ideally new/custom code goes async.

### 2.1 Whitelisted Methods — Both Forms, Auto-Detected

```python
@frappe.whitelist()
def test():          # sync — dispatched via sync_to_async(test) into the thread pool
    return "ok"

@frappe.whitelist()
async def test2():   # async — awaited directly on the loop (native path)
    return "ok"
```

### 2.2 Framework APIs — Async-Native, Sync Fallback

```python
doc = await frappe.get_doc("User", name)   # default, native
doc = frappe.get_doc("User", name)         # fallback — works via asgiref
```

### 2.3 Mechanism — asgiref Both Directions

- **Sync handler → async core**: sync code always runs in the thread pool (never on the loop thread). From there, the sync facade of each API bridges back to the main loop with `asgiref.sync.async_to_sync(async_impl)(...)` — safe because the loop runs in a different thread.
- **Async handler → async core**: plain `await`, zero overhead, no bridge.
- **Detection**: `is_async_callable()` via `inspect.unwrap` decides the path once at dispatch; APIs expose one async implementation + thin `async_to_sync` facade. One code path to maintain.
- **Fail-fast guard**: calling a sync facade from the loop thread (i.e. inside an async handler) raises — asgiref's `async_to_sync` errors when a loop is already running in the current thread. This converts the "async handler silently blocks the loop on a sync call" footgun into an immediate, debuggable exception.
- **Deprecation pressure, not breakage**: sync facade logs a "port to async" hint for custom apps; never removed without a major version.

### 2.4 PostgreSQL: Permanent asgiref Fallback — No Port

Postgres usage is small; lowest priority. **No asyncpg phase.** psycopg2 keeps working indefinitely through the thread pool (`sync_to_async`) behind the same `Database` sync facade — Postgres sites unaffected by the entire migration. Revisit only if real demand appears.

---

## 3. Library Map: Sync → Async

| Today | Async replacement | Phase | Sync survivor |
|---|---|---|---|
| gunicorn/Werkzeug serve | `uvicorn` (programmatic) | 1 | Werkzeug Request/Response kept as parsers |
| `redis-py` (sync) | `redis.asyncio` | 3 | via `async_to_sync` facade |
| `open()` in hot paths | `aiofiles` | 4 | sync call-sites stay (thread pool) |
| — | `aiosqlite` | 5 | — |
| pymysql | `aiomysql` | 6 | pymysql = flag fallback (thread pool) |
| psycopg2 | none — stays | — | permanent thread-pool fallback (§2.4) |
| RQ | SQLite queue (in-loop) / `arq` (scale-out) | 9–10 | RQ = permanent config backend |
| smtplib | `aiosmtplib` | 12 | sync wrapper |
| `requests` (webhooks, integrations, OAuth) | `httpx.AsyncClient` — async-primary call-sites only | 12 | `requests` fine in thread pool, untouched |
| Node socket.io | `python-socketio` AsyncServer | 13–14 | Node removed Phase 18 |

`asgiref` is the bridge everywhere; no other compat shims.

---

## 4. Phase Overview

| # | Phase | Part | Depends on |
|---|-------|------|-----------|
| 0 | Test harness + baseline | Foundation | — |
| 1 | ASGI entrypoint | Foundation | 0 |
| 2 | Async dispatch layer | Foundation | 1 |
| 3 | Async Redis cache | Async core | 1 |
| 4 | aiofiles in hot paths | Async core | 1 |
| 5 | Async DB — SQLite (aiosqlite) | Async core | 1 |
| 6 | Async DB — MariaDB (aiomysql + pools) | Async core | 5 |
| 7 | Async DB — out-of-loop verification | Async core | 6 |
| 8 | `async def` whitelisted methods | Async core | 2, 7 |
| 9 | SQLite task queue alongside RQ | Queue & scheduler | 1 |
| 10 | Flip queue default (RQ stays) | Queue & scheduler | 9 |
| 11 | Scheduler to asyncio | Queue & scheduler | 10 |
| 12 | Async email + outbound HTTP | Async core | 1 |
| 13 | Python socket.io alongside Node | Realtime | 1 |
| 14 | Switch realtime clients | Realtime | 13 |
| 15 | Async ORM — read path | ORM | 6 |
| 16 | Async ORM — write path | ORM | 15 |
| 17 | Async ORM — lifecycle + doc_events | ORM | 16 |
| 18 | Cleanup — remove Node.js | Cleanup | 14 |

```
0 (harness) → 1 (uvicorn) → 2 (dispatch)
1 → 3 (redis) , 4 (aiofiles) , 12 (email + httpx)
1 → 5 (aiosqlite) → 6 (aiomysql+pools) → 7 (CLI verify) → 8 (async whitelist)
1 → 9 (sqlite queue) → 10 (flip default) → 11 (scheduler)
1 → 13 (socket.io shadow) → 14 (switch clients)
6 → 15 (ORM read) → 16 (ORM write) → 17 (ORM lifecycle)
14 → 18 (cleanup: remove Node.js — RQ stays forever)
```

---

## Part A — Foundation

### Phase 0: Test Harness + Baseline

De-risks every later phase. No production code changes.

- Async test support: pick and wire the runner (pytest-asyncio or equivalent inside frappe's test runner) so `async def` tests run alongside sync tests. "All tests green every phase" needs this first.
- Benchmark harness: `autocannon -c10` against homepage, scripted + recorded per phase. References from this bench: gevent+ASGI ~119 rps / p50 82ms (4 workers); WSGI-serial 16 rps. Regressions become numbers, not vibes.
- Spike test: `frappe.local` (ContextVar) propagation across `sync_to_async(thread_sensitive=False)` → `async_to_sync` round-trips — set in sync code, read on loop, and back. The whole compat strategy rests on this; prove it before Phase 1.

**Usable after:** yes — nothing shipped, everything measured.

### Phase 1: Single-Process ASGI Entrypoint (sync everything underneath)

Smallest possible cutover: one process serves, everything else unchanged. **Reuse Werkzeug — do not hand-roll Request/Response compat.** Werkzeug's Request is just an `environ` parser; it doesn't need a WSGI server.

- `app.py` entrypoint: builds the loop, starts `uvicorn.Server(...).serve()` as a task — later phases add queue/scheduler/socket.io tasks to the same loop via ASGI lifespan (`startup`/`shutdown`).
- `frappe/asgi.py` ASGI callable (no Starlette) — raw `scope`/`receive`/`send`.
- **Environ adapter**: build a WSGI `environ` from the ASGI scope (body buffered/streamed from `receive` into a readable stream) and feed the existing Werkzeug-based handler **unchanged**. No custom Request class, no `python-multipart` — Werkzeug already parses multipart, content negotiation, range, conditional requests from environ. A native ASGI Request is a later optimization, only if profiling demands it.
- **Response conversion**: Werkzeug `Response` → ASGI `http.response.start`/`body` sends (status, headers, body, cookies). Must handle **streamed/iterator bodies** (file downloads, `direct_passthrough`) by iterating and sending chunks — never materialize.
- Run the wrapped handler via `sync_to_async(handler, thread_sensitive=False)` on the sized pool (§1.3).
- **`finally` on every path**: call `.close()` on the Werkzeug response iterator AND `frappe.destroy()` — known pitfall from this bench: asgiref never closed the WSGI iterator → leaked 1 DB conn/request → `1040 Too many connections`.

**Usable after:** yes — identical behavior, new server, `python3 app.py` boots it. Drop gevent from serve path.

### Phase 2: Async Dispatch Layer

Routing knows about async, but no handler is async yet.

- `is_async_callable()` via `inspect.unwrap` — handles `@wraps`, `functools.partial`, `__call__`, `@frappe.whitelist()` chains.
- `dispatch()`: async handler → `await`; sync handler → `sync_to_async(...)` into the pool.
- Wire `handle_rpc` / `handle_rest` through `dispatch()`.

**Usable after:** yes — all handlers still sync, just routed through dispatch.

---

## Part B — Async Core (cache, files, DB, handlers, email)

### Phase 3: Async Redis Cache

- Cache layer on `redis.asyncio`.
- Async-primary API: `await frappe.cache().get_value(...)`.
- Sync compat via asgiref `async_to_sync` facade (§2).
- No-loop case (bench CLI, patches call cache too): `async_to_sync` spins up a loop — verify, same as the DB phases.

**Usable after:** yes — sync cache calls keep working through wrapper.

### Phase 4: aiofiles in Hot Paths

- `aiofiles` for file uploads, site config reads, file writes in request path.
- Sync call-sites untouched (they run in thread pool anyway from Phase 1).

**Usable after:** yes.

### Phase 5: Async DB — SQLite (aiosqlite) Proving Ground

SQLite is the cheap proving ground for the async `Database` class — simplest driver, no pooling, validates the async-first pattern + sync facade end-to-end before touching MariaDB.

- `Database` class async-first: primary `async def get_value(...)` etc.
- SQLite backend on `aiosqlite` — per-site connection, WAL mode. No pool.
- Sync compat: asgiref `async_to_sync` facade on every public method — existing sync code calls `frappe.db.get_value(...)` unchanged (§2).
- Port transaction semantics: `commit()/rollback()/savepoint`, `after_commit` hooks.

**Usable after:** yes — flag-gated per site.

### Phase 6: Async DB — MariaDB (aiomysql + per-site pools)

MariaDB/MySQL is where Frappe usage actually is. Async-first `Database` shape already proven in Phase 5 — this phase is the driver + pooling.

- MariaDB/MySQL backend on `aiomysql`, same async-first `Database` class + sync facade.
- **Connection pooling — per-site aiomysql pools** (avoid opening too many connections):
  - Each site = separate MySQL database + own credentials → one `aiomysql.create_pool()` per site, registry `_pools: dict[site, Pool]`.
  - **Lazy creation**: pool made on first request to a site, guarded by `asyncio.Lock` (two concurrent first-requests must not create two pools).
  - **Sizing**: `minsize=0`, `maxsize` from site config (default ~5–10). Many-site benches: total conns bounded by `Σ maxsize`; idle-evict pools for cold sites (close pool after N min unused) so cold sites hold zero connections.
  - **Acquire timeout**: bounded wait on pool acquire — full pool under load returns 503, never deadlocks the request.
  - **Per-request lifecycle**: `frappe.connect()` acquires conn from the site's pool into `frappe.local.db`; held for request duration (transaction-per-request semantics preserved); `frappe.destroy()` rolls back uncommitted state then releases back to pool — never return a dirty connection.
  - **Session state**: Frappe sets `sql_mode`/session vars at connect — must re-apply on acquire (or verify pool reuse keeps them); `pool_recycle` + ping to drop stale conns past MySQL `wait_timeout`.
  - **Shutdown**: lifespan shutdown closes all pools (`pool.close()` + `wait_closed()`).
- aiomysql maturity risk: gate behind site config flag; **fall back to pymysql via thread pool** (`sync_to_async`) if issues.

**Usable after:** yes — flag-gated per site, pymysql fallback one flip away.

### Phase 7: Async DB — Out-of-Loop Verification (CLI/bench/patches)

CLI/bench/patches/migrate run outside the event loop — `async_to_sync` handles the no-loop case (spins one up). Dedicated phase because the surface is wide and failures are subtle.

- Verify `bench migrate`, `bench console`, patch runner, `bench execute`, scheduler-invoked code paths against both aiosqlite and aiomysql backends.
- Verify pool lifecycle outside lifespan (CLI creates + closes its own loop — pools must not leak).

**Usable after:** yes — pure verification + fixes.

### Phase 8: Allow `async def` Whitelisted Methods

First user-visible async feature. **Deliberately after the DB phases** — an async handler shipped before awaitable APIs exist would call sync `frappe.db` directly on the loop thread and silently block the whole process. Now the sync facade raises from the loop thread instead (fail-fast guard, §2.3), and there are real `await frappe.db.*` / `await frappe.cache()` APIs to call.

- `@frappe.whitelist()` accepts `async def` (detection from Phase 2 already works) — both forms side by side, §2.1.
- Document `asyncio.gather()` pattern for parallel calls inside async handlers.
- Dev mode: `loop.slow_callback_duration` warning enabled — any remaining loop-blocking call shows up in logs.
- Sync handlers untouched — `sync_to_async` path, third-party apps need zero changes.

**Usable after:** yes — opt-in async endpoints; everything else as before.

### Phase 12: Async Email + Outbound HTTP

Only depends on Phase 1 — can run parallel to queue work.

- `frappe.sendmail()` async-primary via aiosmtplib.
- Sync compat wrapper — existing call-sites unchanged.
- Same pattern for outbound HTTP: `httpx.AsyncClient` for async-primary call-sites (webhooks, integrations); existing `requests` code untouched — runs in thread pool.

**Usable after:** yes.

---

## Part C — Queue & Scheduler

### Phase 9: SQLite Task Queue Alongside RQ

New queue ships next to RQ — no removal yet.

- `jobs` table: queue/func/kwargs/status/retries/error/timestamps; indexes `(status)`, `(queue, status)`.
- WAL mode, `busy_timeout=5000`, 3–4 in-process asyncio workers, `asyncio.Event` wake signal.
- Retry `max_retries=3`; failed jobs keep traceback.
- Atomic job claim — research's fetch-then-update has an `await` between SELECT and UPDATE, so two in-loop workers can grab the same job; use single `UPDATE ... SET status='running' WHERE id = (SELECT id ... LIMIT 1) RETURNING ...` (requires SQLite ≥ 3.35).
- Workers are asyncio tasks in the main loop (started at lifespan startup) — same process as HTTP, shared memory, no worker process.
- Executes async funcs (awaited) and sync funcs (`sync_to_async` into the shared pool — never `thread_sensitive=True`, or jobs serialize with web requests).
- `frappe.enqueue()` gains backend switch (site config): `rq` (default) | `sqlite`. API identical.

**Usable after:** yes — RQ remains default; opt in per site.

### Phase 10: Flip Queue Default to SQLite (RQ stays, permanently)

- Flip **default** backend to SQLite queue — the simplest thing for a fresh light-mode install: `python3 app.py`, no worker processes, no queue Redis.
- **RQ is a permanent, configurable backend** — large deployments set `queue_backend: rq` in site config and run `bench worker` processes as today. Not deprecated, not removed, ever — light vs heavy is a config choice.
- ARQ as a third opt-in backend for asyncio-native scale-out (multi-server, 10k+/sec, cron, arq-dashboard) — same `frappe.enqueue()`.

**Usable after:** yes — `frappe.enqueue()` unchanged for all callers, one flag picks the backend.

### Phase 11: Scheduler to asyncio

Own phase — scheduler has its own semantics, not a queue footnote.

- Port scheduler tick loop to an asyncio task in the main loop (lifespan startup).
- Preserve: tick interval/lock semantics, per-site iteration + enqueue, missed-tick/catch-up behavior, `scheduler_disabled` flags.
- Old scheduler process remains runnable until cleanup phase — config flip reverts.

**Usable after:** yes.

---

## Part D — Realtime

### Phase 13: Python Socket.IO Alongside Node.js

Runs in same uvicorn loop; Node.js stays up during transition.

- `frappe/realtime_server.py`: `socketio.AsyncServer(async_mode="asgi")`.
- Connect handler: site namespace validation, origin check, cookie/auth parse, direct `frappe.realtime.get_user_info()` call — no HTTP round-trip.
- Port handlers: `ping`, `doctype_subscribe`, `doc_subscribe`, `doc_open`, `doc_close`, `task_subscribe`, `disconnect`, doc-viewer notify.
- Mount in ASGI app: `/socket.io` → `socketio.ASGIApp(sio)`, else HTTP app. One process, two tasks, one loop.
- Keep Node.js running on its port; nothing switches yet.

**Usable after:** yes — Node.js still serves clients.

### Phase 14: Switch Realtime Clients to Python (Node.js stays runnable)

- `publish_realtime()` → direct `loop.create_task(sio.emit(...))`; `after_commit` buffering via `frappe.db.after_commit`.
- Direct emit is safe by design — single process, single loop (§1.1), all clients connected to this process. No Redis pub/sub.
- (Only if someone opts into multi-process scale-out later: sticky sessions + socketio `AsyncRedisManager`. Not the default, not this phase.)
- Point clients at Python server. **Node.js stays installed and runnable** — config flip reverts (rollback rule). Removal in cleanup phase.

**Usable after:** yes.

---

## Part E — ORM (2700+ lines — split read / write / lifecycle)

### Phase 15: Async ORM — Read Path

- `await frappe.get_doc(...)` is the default; sync `frappe.get_doc(...)` keeps working via `async_to_sync` facade (§2).
- `frappe.get_all` / `get_list` / `get_value` on Document level, same dual shape.
- Sync callers in custom apps work unchanged — port at their own pace.

**Usable after:** yes.

### Phase 16: Async ORM — Write Path

- `await doc.save()` / `insert()` / `delete()` async-primary; sync compat wrappers.
- Controller hooks (`validate`, `on_update`, …) through `dispatch()` — async or sync controllers both work; third-party sync controllers unmodified.

**Usable after:** yes.

### Phase 17: Async ORM — Lifecycle + doc_events

- `submit` / `cancel` / workflow transitions async-primary.
- `doc_events` hooks dispatched via unwrap detection — sync hooks unchanged.
- Verify no deadlocks mixing sync/async ORM calls in one request.

**Usable after:** yes — full async ORM, sync API still passes existing suite.

---

## Part F — Cleanup

### Phase 18: Cleanup — Remove Node.js realtime

The only phase allowed to break rollback — runs last, after Python socket.io has proven stable in production.

- Remove Node.js `socketio.js` + `realtime/` — Python server covers both light mode (direct emit) and scale-out (opt-in `AsyncRedisManager`).
- **RQ and `bench worker` are NOT removed** — permanent backend for large deployments (Phase 10).
- Light mode after this: `python3 app.py` runs everything in one process.

**Usable after:** yes — nothing left depends on what's removed.

---

## 5. Cross-Cutting (every phase)

| Concern | How |
|---------|-----|
| Third-party app compat | `asgiref.sync_to_async` wraps all sync handlers/hooks; sync wrappers on every async-primary API |
| Concurrency | `thread_sensitive=False` + sized thread pool everywhere — `thread_sensitive=True` is the 16-rps trap, never use it (§1.3) |
| `frappe.local` consistency | ContextVar-backed; asgiref copies context into pool threads and propagates back (proven in Phase 0 spike) |
| No-loop contexts (CLI/bench/patches) | `async_to_sync` spins up a loop; verified per backend in Phase 7, applies to cache (Phase 3) too |
| Postgres | psycopg2 via thread pool, permanently — same sync facade, no port (§2.4) |
| Large deployments | RQ + `bench worker` + multi-process stay supported via config, permanently — light mode is the default, not the only mode (§1.2) |
| Testing | Parallel test suite — sync + async paths both green before each phase ships (harness from Phase 0) |
| Benchmarks | `autocannon -c10` homepage per phase, compared against recorded baseline (Phase 0) |
| Memory tracked per phase (RSS) | Capture resident memory alongside rps every phase — baseline = Σ today's gunicorn + RQ + Node procs; per phase = single-proc idle + under load (Phase 0). Headline win is ~10–15 procs → 1; every later lever measured against it |
| Process longevity / memory hygiene | Single long-lived process loses gunicorn's `max_requests` recycling → heap fragments. `MALLOC_ARENA_MAX=2`, jemalloc/tcmalloc preload, periodic `gc.collect()` + `malloc_trim`, optional RSS soft-restart (needs Phase 9 reaper). Thread pool sized/shrunk per phase; conn pools conservative + idle-evicting (§Phase 6); SQLite `cache_size` set (§Phase 5/9) |
| Rollback | Each phase flag-gated or additive; previous behavior one config flip away — Phase 18 (Node.js deletion) is the sole, final exception |
