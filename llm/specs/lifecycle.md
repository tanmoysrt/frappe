# Process lifecycle — light-mode server

Reference (not a phase): how one `python -m frappe.serve` process boots, what
threads and asyncio tasks it runs, how a request flows, and how it shuts down.
File:line anchors are to `apps/frappe/frappe/`. See the numbered phase specs for
the *why* behind each piece; this is the *what runs when*.

## One process, one loop, three thread roles

The whole stack is a single process (no gunicorn workers, no Node). Inside it:

- **Main event loop thread** — created by `asyncio.run(_main())` in
  `serve.py:_main`; uvicorn drives it. Owns: the listening socket, HTTP/ASGI
  dispatch, socket.io, the scheduler task, the sqlite queue worker tasks, the
  idle-trim task. Must never block on sync DB I/O (Phase 8 loop guard raises).
- **Bridge loop thread** — a second event loop in a daemon thread
  (`dispatch.py:get_bridge_loop`), started lazily on first use, one per pid. All
  async DB driver coroutines + pooled connections live here, so a request's
  transaction stays on one loop. Sync callers reach it via `run_coroutine_sync`
  (asgiref `async_to_sync`-style).
- **Thread pool** (`ThreadPoolExecutor`, `serve.py:_main`) — runs all *sync*
  frappe work (the WSGI-style request path, ORM, hooks) off the loop via
  `sync_to_async(thread_sensitive=False)`. Sized GIL-aware + lean-capped
  (`_lean_backends` → 4 for sqlite); small stacks (`asgi_thread_stack`, 512 KB).

Heavy RQ workers are a **separate process** (`bench worker`) — they fork, which
must not happen on the web loop (see the `mariadb/aio.py` fork quarantine).

## 1. Boot — `serve.py:main()`

```
python -m frappe.serve
  └─ main()
       ├─ _reexec()      # stdlib only, BEFORE any `import frappe`
       ├─ os.environ["SITES_PATH"] = <bench>/sites ; os.chdir(sites)
       └─ serve(sites_path=".")
```

**`_reexec()`** reads `common_site_config.json` (raw json) and decides the
allocator/GIL env that can only be set at process start:
- `MALLOC_ARENA_MAX=2` (cap glibc arenas; this process runs for weeks),
- `LD_PRELOAD=tcmalloc` iff `use_tcmalloc` (default: lean→0, heavy→1, Phase 25.2),
- `PYTHON_GIL` iff a free-threaded build + `free_threading` + `use_async_db`
  (Phase 23/28).

If the running env already matches (e.g. the Procfile pre-set
`MALLOC_ARENA_MAX`), **no re-exec** → frappe imported once. Otherwise one
`os.execve(python, ["-m", "frappe.serve"], env)`.

## 2. Runtime setup — `serve.py:serve()` → `_main()`

- read knobs from `common_site_config.json` only (`asgi_pool_size`,
  `asgi_limit_concurrency` default 16, `webserver_port` default 8001,
  `asgi_thread_stack`, `malloc_trim_interval`, `loop_debug`, `log_level`),
- install the sized `ThreadPoolExecutor` as the loop's default executor,
- start `_idle_trim()` as a task,
- build `uvicorn.Config("frappe.asgi:application", lifespan="on", ...)` and
  `await uvicorn.Server(config).serve()` — this hands the loop to uvicorn, which
  emits the ASGI **lifespan** events below.

## 3. Lifespan startup — `asgi.py:_lifespan` (`lifespan.startup`)

Runs once on the main loop, in order:

1. `dispatch.register_loop_thread()` — arm the Phase 8 fail-fast guard.
2. `preload.preload_configured_backends()` — eager-import ONLY the drivers this
   config uses (Phase 24.1); everything else stays cold.
3. `await sqlite_queue.start_workers()` — queue worker **asyncio tasks** (no-op
   cheap unless `queue_backend: sqlite`).
4. `scheduler.start_scheduler_task()` — the always-on scheduler tick **task**
   (Phase 27: decides per tick from config + the cross-process FileLock).
5. `realtime_server.start()` — python socket.io subscriber.
6. `await gc_collect_safe()` then `gc.freeze()` — move the permanent import +
   driver graph out of GC scanning (Phase 24.7). gc runs on the bridge-loop
   thread (Phase 26) so finalizers never touch the loop selector cross-thread.
7. send `lifespan.startup.complete`.

A second `gc.freeze()` happens later, once, inside `_idle_trim` after the first
warmup tick — it captures meta/controllers populated by the first requests.

### Task inventory once started
| task | created in | role |
|---|---|---|
| uvicorn server | `_main` (`serve()` coroutine) | loop driver |
| `_idle_trim` | `_main` | per-`malloc_trim_interval`s: `gc_collect_safe` + one-shot post-warmup `gc.freeze` + `malloc_trim(0)` |
| scheduler tick | lifespan startup | enqueue scheduled jobs per tick |
| sqlite queue workers | lifespan startup | drain the sqlite job queue |
| socket.io | lifespan startup | realtime pub/sub |

## 4. Request flow — `asgi.py:application(scope, receive, send)`

```
scope["type"]?
 ├─ http|websocket + path /socket.io  → realtime_server.application   (same loop)
 ├─ http
 │    ├─ _serve_static(scope, send)    → aiofiles stream /assets, public /files
 │    │                                  (304/range on the loop, no pool hop)
 │    └─ _handle_http(scope, receive, send)
 │         ├─ _read_body (buffer / spill-to-disk for big bodies)
 │         ├─ build frappe Request, run the sync request path in the POOL
 │         │   (sync_to_async); DB I/O bridges to the bridge loop
 │         └─ _send_response / _send_file_response, then _finish_request cleanup
 ├─ lifespan                            → _lifespan (startup/shutdown)
 └─ websocket (non-socket.io)           → close (unsupported)
```

Sync handlers/hooks run inline in the pool thread; `async def`
whitelisted methods / controller methods / doc_events run on the loop
(`dispatch.py` dispatch / dispatch_sync / dispatch_hook). Inside `async def`,
data access uses the awaitable facades (`await frappe.aio.*`,
`await frappe.db.aio.*`, `await doc.aio.*`).

## 5. Shutdown — `asgi.py:_lifespan` (`lifespan.shutdown`)

Reverse order, awaited:
1. `await realtime_server.stop()`
2. `await scheduler.stop_scheduler_task()` (cancels the tick task; per-tick lock
   already released each tick, nothing leaks — Phase 27)
3. `await sqlite_queue.stop_workers()`
4. `await sync_to_async(_shutdown_db_pools)()` — close per-site DB pools (they
   live on the bridge loop, so reached through a pool thread → `run_coroutine_sync`)
5. send `lifespan.shutdown.complete`

`_main` then cancels `_idle_trim` in its `finally`. CLI/worker processes (no
lifespan) close their pools via the `atexit` handler in `mariadb/aio.py` instead.

## Known asymmetry
`_idle_trim` is created in `serve.py:_main`, not in the lifespan, so a launch via
plain `uvicorn frappe.asgi:application` gets the scheduler / worker / socket.io
(lifespan) but **not** the gc/malloc-trim tick. Defensible — `serve.py` is the
managed production launcher (it also owns pool sizing + the malloc re-exec) — but
moving `_idle_trim` into the lifespan would put all background tasks in one place
and make every ASGI launcher equivalent.

## Config flip → behaviour (no restart where noted)
- `in_process_scheduler: 0` — scheduler tick stays alive but skips action; an
  external `bench schedule` takes over; honoured next tick (Phase 27).
- `use_async_db: 0` — sync DB drivers, no bridge loop needed (Phase 28 rollback).
- `queue_backend` / `cache_backend` — which workers/driver preload at startup
  (needs restart; these are process-shape knobs).
