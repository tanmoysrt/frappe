# Phase 26: Server logic into the framework + common-config-only knobs

**Part:** Cleanup (follow-up to Phases 20/24/25)
**Depends on:** Phase 25 complete.
**Usable after:** yes — `bench serve` and `python app.py` both unchanged for the user.

## Goal

Two structural cleanups the user asked for:

1. **Move the bench-root `app.py` server logic into the framework** so it is
   versioned and managed with the rest of frappe (bench-root `app.py` was an
   untracked file — easy to drift, hard to manage).
2. **Drop env-var knobs.** Config comes from `sites/common_site_config.json`
   only; `FRAPPE_*` overrides are gone.

## Constraint that shapes the split

`MALLOC_ARENA_MAX` / `LD_PRELOAD` / `PYTHON_GIL` only take effect at process
start, and **any `import frappe.*` runs `frappe/__init__`**. So the allocator/GIL
decision + the one-time `os.execve` re-exec must run *before* the heavy frappe
imports. The first cut kept that bootstrap in a frappe-free bench-root `app.py`.
**26.2 moved it inside the framework** (`frappe/serve.py`): the re-exec helper is
pure stdlib and runs in `main()` before `serve()` does the heavy imports, and the
re-exec is conditional — it only `execve`s when the running env doesn't already
match (so the Procfile's pre-set `MALLOC_ARENA_MAX` lets the common lean path
import frappe exactly once). See 26.2 below; the bench-root `app.py` is gone.

## Changes

### `frappe/asgi.py:serve()` — now the one server core
Folded the bench-root app.py runtime in (it was already documented as "the
framework-owned replacement … bench-root app.py adds malloc tuning on top of the
same core"). `serve()` now owns:
- GIL-aware default pool + Phase 24.10 lean floor (`min(default, 4)` on the
  sqlite + memory-cache + sqlite-queue backend set).
- `limit_concurrency` default `8 * CPU` (CPU-based, not pool-based — no-GIL
  shrinks the pool but serves more rps).
- Phase 24.7 idle-trim task: `gc.collect` + one-shot post-warmup `gc.freeze` +
  `malloc_trim(0)` every `malloc_trim_interval` s.
- boot banner (pool / limit / port / gil status), `loop_debug` slow-callback log.
- `knob(key, default)` reads `frappe.get_common_site_config(sites_path)` **only**
  — no env. Keys: `asgi_pool_size`, `asgi_limit_concurrency`, `webserver_port`,
  `asgi_thread_stack`, `malloc_trim_interval`, `loop_debug`, `log_level`.
- `bench serve` (`frappe.app.serve` → `frappe.asgi.serve`) keeps its `port`
  default 8000; light-mode `serve()` defaults `webserver_port` 8001.

### Bench-root `app.py` — frappe-free re-exec shim only
Reduced to: read common config (stdlib json), decide `use_tcmalloc` (lean → 0,
Phase 25.2) / `MALLOC_ARENA_MAX` / `PYTHON_GIL` (Phase 23), `os.execve` once with
the marker, then `from frappe.asgi import serve; serve(sites_path=".")`. No
`FRAPPE_*` env knobs — common config only. Single frappe import (in the
re-exec'd child).

## Verification

- `serve` imports; `python app.py` boots (banner prints), binds 8001, app
  startup complete, clean shutdown.
- `/api/method/ping` 200 on test.local, minimal.local, sqlite-async.local (Host
  header) — serving parity.
- `test_import_budget` 4/4 green (`frappe.app` probe unchanged — function-level
  `ctypes`/`gc` imports in `serve()` don't touch module import).
- No `FRAPPE_POOL_SIZE`/`_PORT`/`_USE_TCMALLOC`/… references left in `app.py` or
  `asgi.py`.

## Follow-up fix — gc-on-wrong-thread wedges the async-mariadb bridge loop

Surfaced by `bench serve` on `use_async_db: 1` + mariadb + desk socket.io:
`readexactly() called while another coroutine is already waiting`,
`2013 Lost connection`, `(0, 'Not connected')`, then a dead DB.

**Root cause (reproduced via `scripts/repro_async_race.py` + faulthandler):** a
bare `gc.collect()` on any non-bridge thread finalizes cyclic aiomysql
`Connection`/transport garbage; its teardown chain
(`Connection → StreamWriter → transport → loop._remove_reader/_remove_writer`)
mutates the bridge loop's selector. asyncio selectors are not thread-safe →
epoll corrupted mid-I/O → the single bridge loop wedges in
`selector_events.write` → every DB call blocked on `.result()` forever. Same
hazard mariadb/aio.py:127 documents for fork, here triggered by the consolidated
`_idle_trim`'s 30s `gc.collect()` running on the main uvicorn loop thread
(latent in the old bench-root app.py too).

**Fix:** `frappe.dispatch.gc_collect_safe()` — awaitable that runs the collect
(and its finalizers) on the bridge-loop thread (`run_coroutine_threadsafe` +
`wrap_future`), inline fallback when no bridge loop runs (sqlite light mode:
aiosqlite is a worker thread + queue, no loop selector, no hazard). `asgi.py`
`_idle_trim` and the lifespan-startup collect call it instead of bare
`gc.collect()`.

**Verified:** repro control hard-hangs, `gc_collect_safe` 0 failures; new
regression `test_async_db_mariadb.test_gc_collect_safe_does_not_wedge_bridge_loop`
(deadline-guarded, daemon threads) green; live server 5780/5780 → 200 under load
with `malloc_trim_interval=3` (gc tick firing mid-load); unit category 123/123.

## Rollback

`frappe/asgi.py:serve()` is one function; bench-root `app.py` is one file. Revert
both to restore the split (bench app.py running its own uvicorn). The
`gc_collect_safe` fix is one helper in dispatch.py + two call sites in asgi.py.

## 26.2 — Server fully inside the framework + fixed concurrency

- Created `frappe/serve.py` = re-exec bootstrap (was bench-root `app.py`) +
  `serve()` runtime (moved from `asgi.py`) + `main()`/`__main__`. `asgi.py` is
  now just the ASGI `application` + lifespan. `frappe.app.serve` → calls
  `frappe.serve.serve` (runtime, no re-exec — dev). Bench-root `app.py` deleted.
- Launch: `python -m frappe.serve`; Procfile `web: MALLOC_ARENA_MAX=2
  env/bin/python -m frappe.serve`. Re-exec is conditional (only when env must
  change), so the lean Procfile path imports frappe once. Re-exec hardcodes
  `[python, -m, frappe.serve]` (a by-path re-exec would shadow stdlib
  locale/email with frappe's submodules).
- `asgi_limit_concurrency` default `8*CPU` → fixed `16` (common-config override
  still applies). Pool sizing unchanged.
- Verified: `-m` boot + re-exec (tcmalloc) + no-re-exec path + `bench serve`, all
  200; import_budget/dispatch/async_db_mariadb green; unit category 123/123.
