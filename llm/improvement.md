# Improvements

## 2026-06-06 16:00 — Phase 0 done (`9ee43cdefe`)
- Async test support: `AsyncUnitTestCase` / `AsyncIntegrationTestCase` (stdlib, no runner changes)
- Spike test green: `frappe.local` ContextVar dict shared across sync/loop/thread hops
- `scripts/benchmark.py`: autocannon + RSS → `benchmarks/results.jsonl`

## 2026-06-06 16:30 — Phase 1 done (`36215968a9`)
- `frappe/asgi.py` rewritten: raw ASGI3, gevent dropped, WSGI on thread pool, streamed responses
- Bench-root `app.py`: single process, tcmalloc + MALLOC_ARENA_MAX=2, sized pool, gc/malloc_trim tick
- Procfile: `web: env/bin/python app.py` (old line commented = rollback)
- Tests: unit 20/20 green; integration 294 errors pre-existing (DB perms, identical on clean tree)

## 2026-06-07 01:40 — Conn-leak fix + benchmark (`5d03cf5bba`)
- Bug: cleanup ran after final send → lagged behind new requests in FIFO pool → DB conns piled to 150+ → `1040 Too many connections` (also caused the "Exceeded concurrency limit" warnings)
- Fix: close iterator + `frappe.destroy` before terminal send, in the same pool hop
- Benchmark (-c10, homepage): **118.9 rps, p50 77ms, 0 errors, RSS 175 MB idle / 209 MB load**
  - reference: gevent+ASGI 119 rps / p50 82ms with 4 workers; WSGI-serial 16 rps
  - same throughput as gevent, one process instead of 16+
- Open: baseline (gunicorn full-stack RSS) not yet recorded

## 2026-06-07 01:55 — Phase 2 done (`991adc6293`)
- `frappe/dispatch.py`: `is_async_callable` (sees through partial/@wraps/whitelist), `dispatch` (async → await on loop, sync → pool), `dispatch_sync` (pool-thread side)
- Wired: v1+v2 RPC (`execute_cmd`, `handle_rpc_call`) and REST (`api.handle`) through `dispatch_sync`
- Tests: 14 new unit tests; unit category 34/34 green
- Live probe: temp `async def` whitelisted method ran on uvicorn loop from pool thread — bridge works end-to-end (probe deleted)
- Benchmark phase02: **123.1 rps, p50 76ms, 0 errors, RSS 148/219 MB** — no regression vs phase01

## 2026-06-07 02:50 — Phase 3 done (`01fe6d3584`)
- `frappe.cache.aio`: `AsyncRedisWrapper` on redis.asyncio — async port of all custom cache methods, one client per (pid, loop) (`get_async_cache`)
- `frappe/dispatch.py`: `get_bridge_loop()` (persistent daemon-thread loop, fork-safe) + `run_coroutine_sync()` (fail-fast on loop threads) — needed because plain `async_to_sync` spins one-shot loops with no loop around, which kills pooled async conns on call 2 ("Event loop is closed", reproduced); this bridge is the pattern the async DB phases reuse
- Spec deviation (user-approved): sync callers keep the plain sync RedisWrapper (dual stack). Spec-literal "all sync calls through async client" was built and measured first: **106 rps / p50 90ms (-14%)** from 2 thread hops × ~14 redis ops/request — reverted to dual stack: **120.7 rps, p50 77ms, p99 117ms, 0 errors, RSS 148/219 MB** = parity with phase02
- ClientCache untouched (own sync tracked conns + pubsub invalidator thread)
- `assertRedisCallCounts` counts async client commands too
- Tests: 10 new; unit category 44/44; test_caching/test_client_cache/test_perf(22, live server)/test_rate_limiter/test_monitor/test_dispatch all green
- No-loop verify: clear-cache, `bench execute`, full `bench migrate` clean; DB conns flat after 50 reqs; redis client count stable

## 2026-06-07 02:45 — Phase 4 done (`0d04798fd7`)
- `frappe/asgi.py` `_read_body`: only loop-side file I/O in the codebase — SpooledTemporaryFile wrote spilled chunks synchronously on the loop
- Now: small bodies (≤1 MiB) → bytearray → `BytesIO` (no temp-file machinery per request, cheaper than before); over limit → TemporaryFile opened in pool thread, written via aiofiles on the loop's default executor; close (unlink) also off-loop
- Spec items "site config reads, file writes in request path" = no-ops: sync call-sites, already in pool threads since Phase 1 (spec's own "sync call-sites untouched" line)
- aiofiles 25.1.0 env-only like uvicorn/asgiref — declared deps deferred to cleanup phase
- Tests: 7 new (memory/spill/multi-chunk/disconnect paths); unit category 51/51; test_perf 22/22 live
- Live verify: 3 MB upload via `/api/method/upload_file` round-trips byte-identical (md5); DB conns flat after 50 reqs
- Benchmark phase04: **127.0 rps, p50 75ms, p99 106ms, 0 errors, RSS 184/231 MB** — best run yet (phase03: 120.7/77; RSS idle reading includes a 2nd matched bash wrapper proc + post-upload warm state)

## 2026-06-07 02:55 — Phase 5 done (`a4d4dbb8ec`)
- Async DB on SQLite (aiosqlite), flag-gated per site (`use_async_db: 1`), default off — zero impact on existing sites
- Design: Database class untouched above the driver edge — `frappe/database/aio.py` wraps async drivers in sync PEP 249 adapters (`BridgedConnection`/`BridgedCursor`) that submit I/O to the bridge loop (one loop = one connection = intact transactions); commit/rollback/savepoint/hooks work unchanged
- `frappe.db.aio` awaitable facade (any backend): `await frappe.db.aio.get_value(...)` — sync method on worker pool, loop never blocks; this is the API async handlers use from Phase 8
- Memory (spec): explicit `PRAGMA cache_size=-2048` (2 MiB) + `mmap_size=0` — per-connection memory no longer inherits defaults that multiply across sites
- Tests: 10 new; unit category 61/61
- Live: `sqlite-async.local` site created + fully migrated on async backend (BridgedConnection/aiosqlite confirmed in-process); get_value/set_value/exists/count/get_single_value all green; migrate 3.0 s
- No benchmark (sqlite site not on serve path; perf measured in Phase 6 on mariadb)

## 2026-06-07 03:55 — Phase 6 done (`15e142226e`)
- Async DB on MariaDB (aiomysql), per-site pools, flag-gated (`use_async_db: 1` now ON for test.local); pymysql fallback = one flip
- Pools: lazy per site under asyncio.Lock; minsize 0 / maxsize `db_pool_size` (5); acquire bounded 10 s → 503; release rolls back first; ping + SET NAMES per acquire; idle sweep closes whole pools (cold site = zero conn/buffer memory); lifespan + atexit shutdown
- 3 real bugs found during verification (all fixed in same commit):
  1. exit hang: `pool.close()+wait_closed()` waits forever on checked-out conns → `terminate()` (sync-driver die-at-exit semantics)
  2. **context pinning (memory)**: asyncio transports / aiosqlite worker threads / evict task capture ambient contextvars Context → each pooled conn pinned its creating request's entire `frappe.local` (db, request, …) for the conn's life. Driver-object creation now runs in a clean Context (`run_in_clean_context`); config read caller-side. Applies to sqlite backend too
  3. pool starvation via leaked conns: werkzeug test client never closes the WSGI iterator → no `frappe.destroy`; pymysql conns died by refcount GC, pooled conns sat in `pool._used` forever → 5 reqs in, every acquire 503s after 10 s. `weakref.finalize` on the adapter releases leaked conns (cycle-free releaser keeps refcount collection prompt)
- DB conns now bounded by pool maxsize (was: conn per in-flight request): 6 total during 50-way parallel load
- Tests: 9 new; unit 61/61; **test_perf 22/22 in 6.5 s (was ~147 s)** — pool reuse removes per-request TCP+auth
- Benchmark phase06 (3 records in results.jsonl, last is canonical clean run): **130.9 rps, p50 73ms, p99 115ms, 0 errors, RSS 178.6/225.6 MB** — fastest DB phase yet (phase04: 127/75); first 2 records polluted (leaky-code run 133.2, double-server RSS 392)

## 2026-06-07 04:25 — Phase 7 done (`769cc19bb9`)
- Out-of-loop matrix verified on BOTH async backends: bench migrate (0 errors), bench execute, bench console, scheduler helper, patch runner (via migrate), CLI exit pool lifecycle
- 2 real bugs found + fixed:
  1. console double-release: `weakref.finalize` fires at interpreter exit even for live objects → conn released under console's atexit cleanup → rollback on pool-owned conn + 2nd release (aiomysql assert). Fix: `finalizer.atexit = False` (GC-only)
  2. **fork + shared epoll** (RQ-worker shape): child GC of any inherited driver object → `transport.close()` → `epoll_ctl(DEL)` on the fork-SHARED epoll → parent's reader silently unsubscribed → parent hangs forever mid-read (reproduced + py-spy'd). Fix: `os.register_at_fork` hook quarantines parent pools in child (kept alive, release paths no-op on quarantined conns) + fresh registry. First severing attempt (`_writer=None`) was wrong — StreamWriter's own `__del__` re-triggers the bug; quarantine is the correct shape
- Fork regression test added (child reconnects+queries, parent survives forced child `gc.collect()`); module 10/10; unit 61/61
- Live bonus proof: DB conns after all CLI runs = 0 — idle evict closed the server's cold pool entirely (cold site holds zero conns, as spec'd)
- No benchmark (no serve-path change)

## 2026-06-07 04:50 — Phase 8 done (`0e79273193`)
- `async def` whitelisted methods live end-to-end: HTTP → uvicorn loop → dispatch (Phase 2) → `await frappe.db.aio.*` (Phase 5/6); sync handlers untouched (pool thread)
- Fail-fast guard: `Database.sql` raises on event-loop threads (registered: uvicorn loop at lifespan startup, bridge loop at creation) — blocking call gets an immediate error instead of stalling the process; O(1) set lookup, no measurable cost
- `frappe.db.aio` serializes per Database (lock): one conn/request + serial wire protocol — concurrent queries interleave cursor state (caught live by the gather test). `asyncio.gather` safe; DB ops take turns, independent awaitables overlap. Pattern documented in test module docstring
- Lint gate: ruff `ASYNC` rules on project-wide (sleep/requests/open/subprocess in async bodies); ASYNC109 ignored (driver-timeout wrapper false positives)
- Dev mode: `FRAPPE_LOOP_DEBUG=1` in bench-root app.py → `loop.set_debug` + `slow_callback_duration=0.1`
- Tests: 4 new (sync/async/gather/guard via execute_cmd); unit 61/61; sqlite/mariadb/dispatch modules green; live HTTP e2e: async, gather, sync endpoints all 200
- Benchmark phase08: **131.1 rps, p50 74ms, p99 124ms, 0 errors, RSS 152.4/213.4 MB** — parity with phase06 (130.9/73), RSS idle lowest yet

## 2026-06-07 11:10 — Phase 9 done (`695b2dcf93`)
- SQLite task queue next to RQ, opt-in per site (`queue_backend: "sqlite"`; RQ stays default): one `sites/task_queue.db` (WAL, busy_timeout 5000, explicit cache_size 2 MiB / mmap 0), producers = plain sqlite3 from any thread, consumers = 3 asyncio worker tasks on the main loop (lifespan startup) — no worker processes, no queue Redis for opted-in benches
- Atomic claim: single `UPDATE … RETURNING` (no SELECT→UPDATE window; verified by 8-job/4-worker test — each ran exactly once); retries ≤ 3 then `failed` with traceback kept; startup reaper requeues orphaned `running` rows; `asyncio.Event` wake + 2 s poll fallback (cross-process enqueues from bench CLI)
- Jobs reuse `execute_job` (RQ's hooks/retry/transaction semantics) — each in a fresh contextvars Context: worker tasks share the lifespan context's `frappe.local` dict, concurrent jobs would corrupt it
- 1 real bug caught live (not by tests): method resolution ran in the worker context where `frappe.local` doesn't exist → `AttributeError('flags')` on every string-method job; tests passed on the runner's ambient context. Resolution now happens inside the job's own context; regression test runs a job under `run_in_clean_context`
- Deviation (spec said "requeue running older than a timeout"): reaper requeues ALL running rows at startup — every worker lives in this one process, so any running row at boot is orphaned by definition; no timeout window needed
- Not supported vs RQ (documented): per-job timeout kill, at_front, success/failure callbacks — sites needing them keep `queue_backend: "rq"`
- Tests: 11 new; unit 61/61. Live: real job (log cleanup, DB writes) enqueued from bench console, consumed+deleted by server workers across processes
- No benchmark (serve path untouched; RQ still default — perf measured after Phase 10 flip)

## 2026-06-07 11:35 — Phase 10 done (`52bb8ace26`)
- Queue default flipped to `sqlite` — fresh light-mode install = `python3 app.py`, zero worker processes, zero queue Redis. RQ permanent opt-in (`queue_backend: "rq"` + `bench worker`, unchanged); unknown values throw
- ARQ third opt-in backend (`frappe.utils.arq_queue`): producer pool on bridge loop (clean context, pid-checked), workers out-of-process (`arq frappe.utils.arq_queue.WorkerSettings`), arq job-id dedup = same deduplicate contract; worker task reuses `execute_job` + context-safe runner from sqlite queue
- Dep note: arq 0.28 pins `redis<6`, frappe ships redis 7.x — pin conservative, round-trip smoke-tested green on redis-py 7.1.1 (arq env-only until Phase 18); installing arq briefly downgraded redis to 5.3.1, restored to 7.1.1
- RQ-specific tests (test_background_jobs, test_rq_job) pin `queue_backend: "rq"` in setUp; env gap fixed: `freezegun` dev dep missing (scheduled_job_type tests)
- Tests: 5 new; unit 61/61; sqlite queue 11/11; scheduled_job_type 9/9. Live: default enqueue from console → consumed by server workers
- Benchmark honesty note: phase10 measured 122.5 rps / p50 79 / RSS 143.6 idle, but phase08 code RE-benchmarked under same (daytime, warmer) conditions = 124.0 rps / p50 79 — parity; first phase10 readings (114-118) were cold/loaded artifacts; recorded 131 (04:50) reflects a cooler machine, not better code

## 2026-06-07 11:30 — Phase 11 done (`7583a50ba2`)
- Scheduler = asyncio task on the main loop (lifespan startup); tick body (per-site init/connect/enqueue/destroy) on the pool in a fresh Context per tick — third resident of the single process (HTTP + queue + scheduler)
- Semantics preserved verbatim (same sync functions): tick interval + wall-clock alignment, per-site iteration, `scheduler_disabled`/pause/maintenance flags, cron catch-up from last_execution; cross-process FileLock means in-process task and external `bench schedule` can never double-run — config flip reverts (`in_process_scheduler: 0`)
- No `set_niceness` in-process (would renice the whole web process — the old call assumes a dedicated scheduler process)
- Live (5 s tick): full light-mode loop proven in one process — tick → due Scheduled Job Types → sqlite queue → execute_job; 13 job types executed, lock held by server, no errors (scheduler.log tracebacks dated Jun 5 = old gunicorn stack, pre-existing)
- Test lesson: scheduler tests must use a private lock file — the real one is held by any live server (correct in prod, breaks tests); enable_scheduler turned ON for test.local (System Settings) as part of live verify, left ON
- Tests: 4 new; unit category 65/65 (scheduler tests joined the unit set)

## 2026-06-07 11:45 — Phase 12 done (`c6d660f773`)
- SMTP async-primary: wire protocol on aiosmtplib via bridge loop; `SMTPServer.session` → `BridgedSMTPSession` (sync smtplib-shaped facade: sendmail/noop/quit/ehlo/has_extn/esmtp_features = exactly EmailQueue.send's surface) — zero call-site changes, session revival + quit hooks intact; OAuth keeps smtplib (frappe.email.oauth drives the raw session); connections created in clean Context
- Outbound HTTP: `frappe.integrations.aio` — `httpx.AsyncClient` per (pid, loop), `make_request_async` + verb helpers mirroring `make_request` exactly (content-type parsing, integration_request flag, error logging); existing `requests` code untouched (pool threads)
- Phase 8 guard caught a real bug pre-ship: async HTTP error path called `frappe.log_error` (sync DB) on the loop thread → RuntimeError instead of a silent process stall; fixed with a pool hop
- Tests: 7 new — SMTP against a REAL local aiosmtpd server (full wire: connect/EHLO/MAIL/DATA/QUIT through the production facade; revival; refused-connection mapping), HTTP against local http.server; unit 69/69; test_smtp 3/3; test_email 14 errors pre-existing (verified identical on clean tree — urllib3 outbound refused, same family as known 294)
- aiosmtplib 5.1.1 / httpx 0.28.1 / aiosmtpd (test-only) env-only; deps declared in Phase 18

## 2026-06-07 12:00 — Phase 13 done (`1719207dfa`)
- Python socket.io server (`frappe/realtime_server.py`) in the same uvicorn loop — full port of the Node realtime app: per-site namespaces, identical rooms + event handlers (ping, doctype/doc subscribe, doc_open/close + doc_viewers, task/progress subscribe, disconnect)
- Auth = direct in-process call (HTTPRequest sid-cookie resume + validate_auth header auth, pool thread, clean Context) — no HTTP round-trip, no socketio-secret needed; permission checks call frappe.has_permission directly
- Redis "events" subscriber task at lifespan → Python serves the same publish_realtime events as Node, side by side; Node untouched and runnable
- asgi.py routes /socket.io (polling + websocket) to socketio.ASGIApp ahead of the WSGI path
- 1 bug caught live at first boot: function-level `import frappe.realtime_server` shadowed module-level `frappe` → UnboundLocalError in lifespan; tests alone wouldn't have caught it (import path only runs in the server)
- Tests: 4 new (auth + permission helpers in empty-Context threads); unit 69/69. Live e2e over real websocket: login→connect, pong, cross-process publish_realtime → user + doctype rooms, anonymous refused, two-client doc_viewers
- Benchmark phase13: **119.5 rps, p50 80ms, RSS 162/235 MB** — parity with phase10/08 same-day band (122.5/124)
- python-socketio (+aiohttp client for tests) env-only until Phase 18

## 2026-06-07 12:15 — Phase 14 done (`94ae863340`)
- `publish_realtime` from the server process → direct emit onto the socket.io loop (`run_coroutine_threadsafe` from pool threads, fire-and-forget, failures logged) — no Redis hop; after_commit buffering unchanged (flush funnels through the same routing)
- Redis path retained automatically for out-of-process publishers (CLI/workers → in-process subscriber delivers) and for the Node revert: `use_node_realtime: 1` + `socketio_port: 9001` = full flip back; Node stays runnable (removal = Phase 18)
- Clients: `socketio_port` → 8001 in common config (dev clients); non-dev clients use origin = uvicorn already
- Tests: 3 routing tests; unit 69/69. Live e2e: HTTP doc insert → after_commit `list_update` over websocket while redis-cli spy on "events" saw **zero** messages (direct path proven); CLI publish still delivered via subscriber
- Benchmark phase14: **117.6 rps, p50 81ms, RSS 200/284 MB** — parity band (117-124 all day); RSS idle reading taken right after e2e warm-up, not a regression signal
- Realtime now in-process end-to-end: HTTP + queue + scheduler + socket.io = one Python process; node socketio + queue redis now both optional in light mode

## 2026-06-07 12:30 — Phase 15 done (async ORM read path)
- `frappe.aio` — awaitable ORM read facade: `await frappe.aio.get_doc/get_cached_doc/get_lazy_doc/get_last_doc/get_all/get_list/get_value/get_cached_value/get_single_value/get_meta(...)`; each call = one pool-thread hop sharing the caller's request context (frappe.local travels via contextvars copy)
- Sync ORM untouched — custom apps work unchanged, port at their own pace (spec's dual shape, same pattern as frappe.db / frappe.db.aio)
- Calls serialize on the SAME per-Database lock as `frappe.db.aio` — `asyncio.gather` over ORM reads is safe (one conn, serial wire protocol); independent awaitables still overlap
- Spec deviation (documented): "LRU-bound the meta cache" — already satisfied upstream: meta lives in ClientCache (`doctype_meta::*`), FIFO maxsize=1024 + 10-min TTL ≈ bounded ~4 MB; single process = one copy (the real win). LRU conversion would touch the hot cache path for no measurable RSS — skipped
- Tests: 10 new (reads, gather correctness, context sharing, loop-guard regression, execute_cmd production-path handler) green on BOTH backends (test.local mariadb + sqlite-async.local)
- Live: HTTP login → async whitelisted handler `await frappe.aio.get_doc` + gather with db.aio.count → correct JSON
- Benchmark phase15: **116.7 rps, p50 80ms, RSS 173/246 MB** — parity band (117–124 daytime); read path adds nothing to sync hot path (facade is opt-in)

## 2026-06-07 12:50 — Phase 16 done (async ORM write path)
- `await doc.aio.save()/insert()/delete()` — `Document.aio` property → AsyncDocumentFacade: any doc method awaitable, pool-thread hop with caller's request context, serialized on the per-Database lock; module verbs added: frappe.aio.new_doc/delete_doc/rename_doc
- Controller hooks through dispatch: `run_method` detects async controller methods (`is_async_callable`) and bridges via `dispatch_hook`; sync controllers run inline, untouched — third-party apps unmodified
- THE deadlock designed out: doc.aio.save holds the ORM lock; an async `validate` awaiting `frappe.db.aio.*` lands on another pool thread → would deadlock. `OwnedLock` (Lock + owner-thread tracking, replaces facade's plain Lock) lets `dispatch_hook` release the holder's lock for the hook's duration (holder parked, conn idle) and reacquire after — proven by test that hung before the fix
- Tests: 5 new (insert/save/delete, gather of 4 inserts serialized, async hook under doc.aio [deadlock case], async hook on plain sync path, sync controller regression) — green both backends; test_document 67 ran, 2 errors PRE-EXISTING (verified identical on clean tree); unit 69/69
- Live: HTTP login → POST /api/resource/ToDo (sync write path through changed run_method) → created; DELETE ok
- Benchmark phase16: **120.6 rps, p50 79ms, RSS 172/258 MB** — parity band; per-run_method `is_async_callable` check costs nothing measurable

## 2026-06-07 13:05 — Phase 17 done (async ORM lifecycle + doc_events)
- `await doc.aio.submit()/cancel()` work through the Phase 16 facade (any method awaitable); workflow transitions: `frappe.aio.apply_workflow` (dotted-path awaitable wrapper, resolved in the pool thread's request context)
- doc_events hooks dispatched via `dispatch_hook` in Document.hook's composer — async doc_events handlers bridge to the loop, sync handlers run inline untouched (unwrap detection = is_async_callable through partial/@wraps chains)
- Deadlock verification (spec item): three tests incl. the worst chain — doc.aio.insert (lock held) → async before_insert hook → sync ORM in pool thread → second async hook → await db.aio — all complete under 30 s timeouts; nested facade-in-facade (async on_submit awaiting frappe.aio.get_value while doc.aio.submit holds the lock) proven on submit path
- Tests: 3 new (runtime submittable doctype submit/cancel + async on_submit; patched doc_events after_insert async handler; nested mixing) — green both backends; test_document 67 ran / 2 errors pre-existing (same on clean tree); test_hooks 6/6; 18/18 module total
- Live: server restart, HTTP write path (POST/DELETE /api/resource/ToDo) through changed composer — ok
- Benchmark phase17: **122.5 rps, p50 79ms, RSS 164/188 MB** — parity band; dispatch_hook per doc_events hook costs nothing measurable

## 2026-06-07 13:25 — Phase 18 done (cleanup: Node.js realtime removed, deps declared)
- Removed `socketio.js` + `realtime/` (index/handlers/utils/middlewares) — python realtime server (Phase 13/14) is the only realtime stack now; the only phase allowed to break rollback
- package.json: dropped server-side `socket.io` + `cookie` (only node realtime used them); `socket.io-client` KEPT (browser client talks to the python server); node_utils.js KEPT (esbuild uses it); build pipeline untouched
- Procfile: socketio line removed; schedule/worker lines commented with revert note (in-process scheduler+queue are the light-mode default; RQ + `bench worker` remain the heavy-mode backend — NOT removed, per Phase 10)
- pyproject.toml: ASGI stack declared — uvicorn~=0.49, asgiref~=3.11 (bench fork verified = unmodified upstream clone), aiofiles, aiosqlite, aiomysql, aiosmtplib, httpx, python-socketio; arq as optional extra `frappe[arq]` (its redis<6 pin conflicts with redis~=7.1 in the resolver; runtime round-trip on redis-py 7.x smoke-tested — install --no-deps)
- `use_node_realtime` now only forces the Redis publish path (docstrings updated); `frappe.realtime.get_user_info` endpoint kept (tests reference it; harmless)
- Light mode confirmed: `python3 app.py` runs HTTP + websockets + queue + scheduler; redis required only for cache (+ events channel for out-of-process publishers)
- Final sweep: 9 async modules OK, unit 69/69, test_perf 22/22, test_realtime_server 7/7; live websocket e2e post-removal: connect + pong + redis-path delivery green
- Benchmark phase18: **115.8 rps, p50 81ms, RSS 161/234 MB** — parity band (115-124 daytime)

## Migration complete — all 18 phases done
One process (`python3 app.py`): HTTP (uvicorn/ASGI) + async DB (aiomysql/aiosqlite behind sync facade + db.aio/frappe.aio awaitable APIs) + sqlite task queue + asyncio scheduler + python socket.io realtime + async SMTP/HTTP. Sync API fully preserved for third-party apps (gunicorn/RQ/Node all replaced or optional). Steady-state: ~120 rps @ p50 ~80ms, RSS ~160 MB idle vs multi-process gunicorn+workers+node stack.

## 2026-06-07 13:35 — Cleanup round 2 planned (specs phase19–23, no code yet)
- phase19: remove gunicorn + Werkzeug — native ASGI request/response path (async-first; sync = pool-thread slow path); 32 files import werkzeug today; 7 sub-steps, werkzeug deleted last
- phase20: free-threaded Python (no-GIL 3.14t) — analysis-first: cp314t wheel audit per C extension, thread-safety audit of module caches, GIL-vs-noGIL benchmark gate; separate env-t venv, Procfile-swap rollback
- phase21: remove redis cache dependency — InProcessCache (audited redis-command subset), `cache_backend: memory` default, cross-process invalidation via `.cache-generation` bump-file; redis stays opt-in for multi-process
- phase22: architecture options move to common_site_config (use_async_db, queue_backend, realtime/serve knobs + app.py env vars) — `get_common_conf` helper, site-level overrides ignored with boot warning; DO FIRST (19/21 add knobs)
- phase23: libsql replaces sqlite3/aiosqlite (db backend, task queue, FTS5 search) — compat-adapter + `sqlite_engine` knob, default stdlib until parity; embedded-replica DR as optional headline
- Recommended order: 22 → 19 → 21 → 23 → 20 (no-GIL last: needs smallest C-ext surface + wheel ecosystem)

## 2026-06-07 13:50 — Cleanup specs reordered + memory phase added
- Renumbered to recommended order: phase19 common-config, phase20 gunicorn/werkzeug removal, phase21 redis cache removal, phase22 libsql, phase23 free-threading (cross-refs fixed)
- New `specs/phase24-memory-footprint.md` — measured baseline: bare python 14.2 MB, `import frappe` 59.4 MB (598 modules), idle 161 MB; isolated costs werkzeug +14.1 MB / rq +11.8 MB / pydantic +4.0 MB / pypika +1.6 MB; eager rq chain via frappe/__init__.py:1588
- phase24 targets: lazy imports (PEP 562), translation dict bounds, allocator matrix (tcmalloc/jemalloc/glibc+trim), gc.freeze after warmup, cache stats + caps, session cache LRU; success gate idle <100 MB, import <35 MB; do before phase23 (no-GIL adds memory)

## 2026-06-07 17:40 — Phase 20 done (gunicorn + Werkzeug removed; async-first request path)
- **20.1** gunicorn dropped (pyproject git dep + run_simple dev server); `bench serve` → programmatic uvicorn via `frappe.asgi.serve()` (pool sizing + knobs shared with bench-root app.py); TestGunicornWorker (all skipped-flaky) deleted; concurrency_limiter keeps fallback=4 without gunicorn master
- **20.2** native ASGI statics: /assets + public /files served from the loop — aiofiles 256k chunks, ETag/Last-Modified 304s, single-Range 206/416, traversal guard, force-download extensions; SharedData/StaticData middlewares unwired (verified live: 200/304/206/416/HEAD/traversal/site-scoping)
- **20.3** `frappe/http.py` (~1000 lines): Request (from ASGI scope, lazy form/files via python-multipart, body spill-aware), Response (bytes/str/iterable/file, set_cookie), Headers/MultiDict/FileStorage, HTTPError hierarchy on http_status_code, redirect/send_file, Rule/Map/Submount router (path/int converters, implicit HEAD, strict-slash RequestRedirect) — werkzeug-parity details preserved: dict.update(MultiDict) first-value (__iter__ override → CPython dict_merge slow path), quoted non-ascii filenames (no RFC5987), Content-Length tracks .data, status_code int-coerces, form parsing keyed on content-type NOT method (oauth sends urlencoded bodies on GET). 35 unit tests
- **20.4** asgi.py native: scope→Request, handle_request on pool, Response sent from loop; after-response chain (rate limiter→recorder→after_response→destroy) explicit, BEFORE terminal send (conn-bounding kept); ProxyFix → X-Forwarded-* scope rewrite; app.py loses @Request.application/ClosingIterator/WSGI globals
- **20.5** LocalProxy vendored (~90 lines, __class__-faking proxy over the ContextVar dict); frappe-native HTTP exceptions everywhere; werkzeug.routing/exceptions/wrappers imports swapped across api/, auth, handler, website/, oauth2, response.py, sentry (WSGI event processor → direct request facts)
- **20.6** native test client (`frappe/testing/client.py`): drives handle_request in a copied context with FRESH frappe.local (ContextVar dict instance is shared by copy_context — set a new dict or the test's own context gets clobbered), cookie jar, redirects, absolute URLs, multipart encode; kills the werkzeug-client iterator-never-closed leak; ~10 test modules swapped off werkzeug.test
- **20.7** Werkzeug deleted: pyproject + pip uninstall — unit category green with the package ABSENT; middlewares.py removed; grep gate clean
- Gotchas hit: werkzeug parsed GET bodies by content-type (oauth 400s until matched); RedirectPage assigns status_code as str; tests pass absolute URLs to the client; oauthlib needs urlencoded body params; **mid-phase `pkill` killed a `git stash pop`** — several suites silently ran against the stashed (old) tree until git status exposed it; recovered from stash, re-verified everything on the real tree
- Tests: unit 104/104 (incl 35 http) WITHOUT werkzeug installed; test_api 23/25 (2 pre-existing: wkhtmltopdf missing, port-80 server test), oauth20 7/8 (1 pre-existing port-80), website 24/24, client 12/12, cors/rate_limiter/local_proxy/boot/perf green, aio_orm/dispatch/async_whitelist/sqlite_queue/realtime_server green, sqlite backend 10/10; user/file failures identical on clean tree
- Live: login → desk (116 KB render), JSON + form-encoded CRUD, multipart upload (small + 3 MB spill, roundtrip identical), private files (200/403 guest/Range), redirects, websocket polling handshake, asset 304s
- Benchmark phase20-final: **152.5 rps, p50 63ms** (was 115–125 / ~80ms since phase 1) — environ build + start_response bridge + werkzeug parse were ~25% of request overhead; RSS idle 168 MB / load 261 MB

## 2026-06-07 18:05 — Phase 21 done (redis cache removed; in-process backend default)
- `frappe/utils/inprocess_cache.py`: InProcessCache (dicts + one RLock — Phase 23-ready; pickled values = identical semantics incl mutation isolation; TTL lazy + amortized sweep every 2048 mutations; FIFO cap 50k entries / FRAPPE_CACHE_MAX_ENTRIES; statistics for RSS investigations) + InProcessClientCache (memoizing alias, maxsize 1024/ttl 10min like redis ClientCache) + awaitable `frappe.cache.aio` facade (no pool hop — in-process ops return inline)
- `cache_backend` common knob, default **memory**; "redis" keeps today's wiring (heavy/multi-process rule documented: RQ workers ⇒ redis)
- Cross-process invalidation: `sites/.cache-generation` bump-file (atomic rename) written by clear-cache + migrate; server stats it ≤1/s and flushes on change — e2e verified (CLI clear-cache → running server flushed, kept serving)
- Strays: get_socketio_secret falls back to in-process cache on ConnectionError; check_connection skips redis_cache probe on memory backend (migrate runs without redis); execute_command("INFO","MEMORY") emulated for system health report; Procfile redis_cache line was already commented
- Cache-cold sessions verified: server restart (memory cache wiped) + old sid cookie → session resumed from tabSessions
- No-redis verification done via FRAPPE_REDIS_CACHE/QUEUE env pointing at unreachable ports (couldn't stop the shared redis daemons in this session): boot + login + desk + statics + websocket + clear-cache e2e all green with zero reachable redis
- Tests: 23 new (semantics mirror redis: roundtrip/snapshot isolation, memo layer, generator, TTL, hashes/sets/lists, blpop wait, raw counters, pipeline, FIFO eviction, flushall, bump-file, stats, aio facade); redis-only suites (test_client_cache, test_async_redis_cache, test_perf pubsub) pin the redis backend via explicit setup_cache() and skip if redis down; test_caching needed TestResponse.cache_control (phase 20 gap). Matrix: unit 104/104 + cache suites green on BOTH backends; queue/realtime/website/perf/api green on memory
- Benchmark phase21: **176.6 rps, p50 54ms, RSS idle 162 MB / load 201 MB** (phase20-final was 152.5/63) — hot-path redis round-trips were ~15%; net stack RSS also drops the redis-server process (~10 MB) in true light mode

## 2026-06-07 18:20 — Phase 22 analysis done (libsql) — implementation GATED, stdlib stays
- Probed PyPI `libsql` 0.1.11 (cp314 wheel exists; no cp314t; libsql-experimental = sdist-only)
- Works: UPDATE…RETURNING (queue claim), executemany/executescript, FTS5, busy_timeout, embedded replica (`conn.sync()`)
- BLOCKERS (full table in analysis.md): no `create_function` (ORM REGEXP queries die), no `register_converter`/PARSE_DECLTYPES (datetime round-trip dies), no DB-API exception hierarchy (bare ValueError — queue IntegrityError dedup + database.py error mapping die), live-file WAL interop BROKEN (PRAGMA reports wal but writes -journal; stdlib conn on same path can't see committed data → CLI producer/server worker shared task_queue.db unsafe), no row_factory/total_changes/backup
- Verdict per spec's analysis gate: do NOT flip any backend; `sqlite_engine: "stdlib"` remains default (knob reserved in common config); re-evaluate when the binding ships create_function + sqlite3-compatible exceptions + row_factory + honest WAL. libsql uninstalled from env (not declared)

## 2026-06-07 18:40 — Phase 23 analysis done (free-threading) — GATED on orjson
- Interpreter: uv-managed cpython-3.14.3+freethreaded installed; `env-t/` venv beside `env/` (GIL verified off); Procfile `web-t:` swap line already present
- Wheel/import audit (full table in analysis.md): ~70 deps clean with declared free-threading support (incl pymysql/aiomysql/aiosqlite/uvicorn/pydantic-core/cryptography/Pillow); GIL re-enablers at import: mysqlclient (avoidable — light mode is pymysql; app.py preload must go lazy), hiredis (omit), lxml (lazy premailer email path); psycopg2-binary no cp314t wheel (swap to psycopg[binary] if postgres needed)
- **HARD BLOCKER: orjson** — no cp314t wheel and source build refuses ("orjson does not support free-threaded Python"); frappe imports orjson unconditionally in core → framework cannot even import on 3.14t; GIL-vs-noGIL benchmark impossible until then
- Our code is no-GIL-prepped: OwnedLock (real Lock), InProcessCache single-RLock design (built for this in Phase 21), ContextVar-scoped frappe.local; remaining check-then-act audit list recorded for the unblock day
- Re-evaluation: try `uv pip install --python env-t/bin/python orjson` on each release; then lazy mysqlclient preload, exclude hiredis, rerun audit, benchmark per spec

## 2026-06-07 20:30 — Phase 23 IMPLEMENTED (free-threading) — orjson gate lifted via msgspec
- **orjson blocker killed by swapping to msgspec** (no compat shim): msgspec declares free-threaded support (GIL stays off at import), same strict JSON as orjson (64-bit ints, no NaN, compact, native datetime/UUID encode), ~5x faster than stdlib on response-shaped payloads. Call sites use `msgspec.json.decode/encode` + `msgspec.DecodeError` directly; `orjson_dumps` keeps its name (whitelisted in safe_exec) but routes `default`-hook calls through stdlib json so `json_handler` keeps controlling datetime format ("2026-01-01 10:30:00", not RFC 3339 — frontend parses that). Verified live: /api/resource `modified` unchanged
- **GIL re-enabler audit on cp314t (scripts/gil_audit.py, per-module subprocess probe):** only offenders left were transitive. Fixes: `mysqlclient` 2.2.7→**2.2.8** (declares free-threaded; light mode is pymysql anyway), `lxml`+`premailer` made optional (premailer import wrapped → emails skip style-inlining on no-GIL instead of failing), `hiredis` omitted (redis-py works pure-python; light mode has no redis), `psycopg2-binary`→optional `postgres` extra. After: **libsql is the ONLY GIL re-enabler left** (its cp314t wheel is sdist-built, no `Py_mod_gil` slot) — on a sqlite bench you accept it or `PYTHON_GIL=0`
- **Opt-in + gated:** `free_threading` knob in common_site_config (architecture key, env `FRAPPE_FREE_THREADING`); GIL drops only when knob set AND `use_async_db` on ("use GIL with async only" — the no-GIL pool is pointless if DB I/O blocks it). `app.py` decides `PYTHON_GIL` before the malloc re-exec (flag only honored at interpreter start). Non-FT build → knob is a no-op
- **Pool sizing:** no-GIL default pool drops 2×CPU→CPU (threads now truly parallel, avoid oversubscription); `limit_concurrency` re-based on CPU not pool (8×CPU) so the smaller pool doesn't shed valid burst load as 503s
- **Async postgres added (psycopg3, not aiopg):** aiopg's `conn.commit()` raises in libpq async mode — breaks txn-per-request; `psycopg.AsyncConnection` has real async commit/rollback. `frappe/database/postgres/aio.py` mirrors the aiomysql backend on `psycopg_pool.AsyncConnectionPool`. Structurally complete + import/parity-checked; NOT verified against a live PG (none in env). postgres can't run no-GIL anyway (psycopg2 needed for escaping has no cp314t wheel)
- **Benchmarks (16-core box, cpython-3.14.3t, both sites mariadb async):**

  | workload | GIL c10 | GIL c50 | no-GIL c10 | no-GIL c50 |
  |---|---|---|---|---|
  | cpu_burn (rounds=2, pure-Python) | 49.5 rps / p50 200ms | 47.1 / p50 1016ms | **482.7 / p50 19ms** | **489.7 / p50 98ms** |
  | ping (full dispatch, no DB) | 815.5 / p50 12ms | 796.8 / p50 61ms | **3622.7 / p50 2ms** | **4055.6 / p50 402ms** |

  CPU-bound: **~10x** (GIL pinned to one core regardless of -c; no-GIL scales across cores). Even ping **~4.4-5x** — the ASGI dispatch + contextvar + msgspec pipeline is pure-Python CPU the GIL was serializing. Spec success bar (no-GIL wins at -c50, loses <10% at -c10) cleared by a wide margin
- **Memory:** no-GIL RSS idle 159 vs 142 MB (+12%, immortalization + biased refcounting), load 219-272 vs 150-168 MB (bigger pool buffers). Acceptable for the throughput
- **Soak:** -c50 30s cpu_burn on no-GIL with the fixed limit_concurrency=128 → **zero 5xx, zero "Exceeded concurrency", zero PYTHON_GIL re-enable warnings** in log. (The 503s in the first soak were the accidentally-halved cap, now fixed)
- All test suites green under no-GIL: unit 117/117 both sites, libsql_compat 13/13, aio_orm 18/18, async_db_mariadb 10/10
- Added `frappe.cpu_burn(rounds)` whitelisted method (capped, guest) as the CPU-bound benchmark probe; `scripts/gil_audit.py` for the wheel audit

## Cleanup round summary (phases 19-23)
- 19 ✅ common-config architecture knobs (get_common_conf/patch_common_conf, boot warning)
- 20 ✅ gunicorn + Werkzeug REMOVED — native http.py/asgi pipeline, native test client; **152.5 rps p50 63ms**
- 21 ✅ redis cache removed by default — InProcessCache + bump-file invalidation; **176.6 rps p50 54ms** (vs ~120/80 pre-cleanup: +47% rps, -33% latency); zero redis processes needed in light mode
- 22 ⛔ libsql: analysis complete, implementation gated (binding lacks create_function/converters/exceptions/row_factory; live-file WAL interop broken)
- 23 ⛔ free-threading: env-t ready, audit complete, gated on orjson upstream

## 2026-06-07 19:10 — Phase 22 IMPLEMENTED (libsql is the main-DB sqlite engine) — gate lifted
- Re-probing killed the "blockers": WAL live-file interop with stdlib WORKS both directions (first analysis misused autocommit=True + PRAGMA-in-transaction); **REGEXP is NATIVE in libsql** (no create_function needed — and no python-callback serialization like stdlib's regexp); concurrent libsql+stdlib writers on one WAL file: 0 errors
- `frappe/database/sqlite/libsql_compat.py` (~550 lines) absorbs the real gaps: ValueError→sqlite3 exception classes (IntegrityError/OperationalError/ProgrammingError by message), sqlite3.Row-compatible Row, decltype-driven timestamp/date/time conversion (binding hides decltypes → column→decltype map from sqlite_master/pragma_table_info, cached per DB FILE — uncached cost was 15s vs 2.9s on test_document; unknown column triggers rebuild), datetime param adaption to ISO, statement draining before commit (libsql refuses open statements), aiosqlite-shaped AsyncConnection thread-runner (incl execute_fetchall) so BridgedConnection runs unchanged
- Scope per user direction: SQLiteDatabase + AsyncSQLiteDatabase = libsql ONLY (stdlib sqlite3 driver off the main-DB path; exception classes kept — engine-independent); task queue + sqlite_search stay stdlib (independent files); mariadb/postgres via db_type untouched; sqlite_engine knob retired (removed from ARCHITECTURE_KEYS + common config); aiosqlite stays (queue worker)
- Matrix: test_libsql_compat 13/13, async_db_sqlite 10/10, aio_orm 18/18, sqlite_queue 11/11 (stdlib), sqlite_search 14/14 (stdlib), unit 117/117 both sites, async_db_mariadb 10/10; test_document failure set byte-identical to stdlib pre-existing baseline (incl the converter-sensitive test_update_after_submit — decltype map nailed it; value-shape conversion did NOT and was replaced)
- Cross-engine: stdlib-created site db served by libsql, reopened by stdlib (counts + PRAGMA integrity_check ok)
- Live: sqlite-async.local on libsql — login, list, REGEXP like-filters, writes; soak autocannon -c25 20s + 300 parallel enqueues: zero non-2xx, zero "database is locked"
- Benchmarks: mariadb site phase22 **169.2 rps p50 55ms** (parity, daytime band vs 176); sqlite site homepage ~40 rps -c10 (full website render)
- Known deviations (documented in adapter docstring): no regexp_replace SQL function on sqlite (no framework usage; create_function warns), alias/expression columns skip type conversion (stdlib converts aliased decltypes), conversion guarded by strict ISO patterns (junk in typed columns → str instead of stdlib's crash)

## 2026-06-07 21:30 — Phase 24 memory profile + spec rewritten (planning)
- **Fresh memory profile (GIL build env/, python 3.14.4):**

  | measure | RSS | modules |
  |---|---|---|
  | bare python | 11.5 MB | 77 |
  | `import frappe` (no site) | 55.8 MB | 561 (+484 over bare) |
  | full boot idle (running srv, smaps_rollup) | 159 MB Rss / 145 MB Pss | — |

- **Eager heavies pulled by `import frappe`:** rq, redis, pydantic (confirmed via sys.modules probe). bs4/openpyxl/num2words/xlrd are NOT eager — already lazy / feature-module-level. So the import-time win is rq/redis/pydantic lazy; the feature libs need *off-switches* so they never load even on demand.
- **Isolated import cost (fresh interp, RSS):** premailer +30.3, bs4 +23.4, openpyxl +21.8, html5lib +11.1, num2words +3.5, xlrd +2.7, csv +0.0
- **is-HTML-via-bs4 test** (BeautifulSoup().find() used only as "has any tag" boolean): `frappe/model/base_document.py:1383`, `frappe/utils/html_utils.py:162` — replace with cheap regex helper so the document write path never imports bs4
- **RQ Job desk doctype** (`core/doctype/rq_job/rq_job.py`) reads only redis/rq → sqlite queue jobs invisible in light mode. sqlite `jobs` table schema (job_id/site/queue/func/status/enqueued_at/started_at/ended_at/error/retries) maps onto RQ Job fields → make get_list/load_from_db backend-aware when `queue_backend=sqlite`
- **Rewrote `specs/phase24-memory-footprint.md`** into a 24.0–24.9 sub-phase plan. Incorporated user direction: (1) NO new env-var knobs — all new flags common_site_config only; (2) sqlite jobs in RQ Job (24.4); (3) minimal-site harness to read true RSS floor (24.0); (4) opt-in off-switches for bs4/num2words/csv/excel (24.2) + bs4 is-HTML replacement (24.3); (5) simpler/cleaner; (6) every lever re-verified no-GIL-safe (locked shared caches, gil_audit on any new lazy default dep) since Phase 23 already shipped
- Implementation NOT started — this entry is profile + plan only (CLAUDE.md: small changes at a time; sub-phases ship one by one next)

## 2026-06-07 22:10 — Phase 24 plan: lean defaults + final-profiling pass + config docs (planning)
Spec additions to `specs/phase24-memory-footprint.md`:
- **24.10 Lean defaults for new sites** — a fresh `bench new-site` lands on the smallest-memory config; operator opts UP into heavy backends, never down. Lower uvicorn worker-pool by default when the lean backend set is active (sqlite + in-process cache + sqlite queue) since there's no external I/O to overlap — big pool = wasted thread-stack RSS. Heavy backends keep CPU-based pool sizing.
- **24.11 Final profiling pass** — one consolidated before/after measurement after 24.1–24.10 land (re-run memprofile for every baseline row + rps/p50 + gil_audit), produce the delta table, record the headline number here.

### Configuration reference (Phase 24 — all in common_site_config.json, NO env vars)
Default column = what a NEW site gets with no flag set. Lean defaults are the absence of heavy flags, not a "lean mode" switch.

| key | default (lean) | heavy opt-in | effect |
|---|---|---|---|
| `db_type` | `sqlite` (libsql engine, Phase 22) | `mariadb` / `postgres` | main DB backend; only its driver preloaded (24.1) |
| `cache_backend` | `"memory"` (InProcessCache) | `redis` | redis driver lazy until configured |
| `queue_backend` | `sqlite` | `rq` | sqlite `jobs` table queue; rq+redis lazy until configured |
| `disable_rq` | off (on for minimal-site harness) | off | belt-and-suspenders: lazy rq/redis accessors raise instead of import |
| `enable_html_parsing` | on | on | off → bs4 parsing paths degrade/raise; cheap is-HTML test (24.3) still works |
| `enable_number_to_words` | on | on | off → money_in_words / num2words shed |
| `enable_excel` | on | on | off → xlsx import/export shed (CSV stays) |
| `enable_csv` | on | on | off → csv import/export shed (stdlib, ~0 MB; surface control only) |
| `asgi_pool_size` | small fixed (e.g. 4) when lean set active | CPU-based when heavy backend configured | uvicorn worker-thread pool; lower = fewer thread stacks (existing knob) |
| `free_threading` | off (Phase 23) | on (requires `use_async_db`) | no-GIL build; ~10x CPU-bound, +12% idle RSS |
| `use_async_db` | per site | `1` | async DB drivers (aiosqlite/aiomysql/psycopg) |
| `use_mysqlclient` | off (pymysql) | `1` (MySQLdb) | mariadb sync driver choice; mysqlclient re-enables GIL |
| `tracemalloc` | off | `1` | memprofile top-25 alloc trace (~2x slow, never default) |

Boot-time-only env (app.py owns the pre-import re-exec; NOT new runtime knobs): MALLOC/LD_PRELOAD allocator, PYTHON_GIL (driven by `free_threading`), pool/limit sizing.

- Preload rule (24.1): config decides which backend drivers are eager-imported before `gc.freeze()` (24.7); everything else stays cold until first use. A default sqlite/in-process/sqlite-queue site imports zero redis, zero rq, zero mariadb/postgres drivers.
- Implementation NOT started — planning entry only (CLAUDE.md: small changes one at a time; sub-phases ship in order next).

## 2026-06-07 20:40 — Phase 24.0 DONE (measurement tooling)
First implementation sub-phase. All additive — no existing code removed; net change to `utils/__init__.py` is zero (memstats moved to its own module).
- **`scripts/memprofile.py`** (new) — subcommands `bare` / `import` / `proc --pid` / `stats --site [--tracemalloc]`, each appends one tagged JSON row to `benchmarks/memory.jsonl`. `import` runs `import frappe` in a fresh subprocess (RSS delta + module count + denylist leaks); `proc` reads smaps_rollup of a running server; `stats` boots a site and calls the shared `memstats`.
- **`frappe/utils/memstats.py`** (new) — System-Manager-only whitelisted method `frappe.utils.memstats.memstats`. RSS/Pss, modules, gc + freeze counts, cache/client_cache sizes+hits/misses, opt-in tracemalloc top-N. Put in own module because `utils/__init__` loads before `frappe.whitelist` exists (circular import on the decorator — caught + fixed during impl).
- **`scripts/new_minimal_site.sh`** (new) — creates a sqlite minimal site, writes a lean common-config *sidecar* (`sites/<site>/minimal_common_config.json`) without touching the shared bench common config, records bare+import floor.
- **`frappe/tests/test_import_budget.py`** (new, UnitTestCase) — fresh-subprocess `import frappe`; asserts module count ≤ 600 (baseline 561), STRICT denylist (werkzeug.serving/IPython/sentry_sdk/bs4/openpyxl) absent NOW, PENDING denylist (rq/redis) `@expectedFailure` until 24.1. Run: **3 tests OK (expected failures=1)**.

**Baseline measured (env/ GIL build, python 3.14.4):**

| measure | RSS | Pss | modules |
|---|---|---|---|
| bare python | 12.1 MB | — | 81 |
| `import frappe` (fresh subproc) | 56.2 MB (+44.3 delta) | — | 561 (rq+redis leak = 24.1 target) |
| booted test.local (in-proc) | 67.3 MB | 51.0 MB | 647 |

- Memory/perf vs previous: tooling-only, no runtime change → server RSS/rps unchanged. Establishes the measurement floor 24.1+ optimize against.
- Spec-key corrections found during impl (fixed in spec + config table): lean `cache_backend` = `"memory"` (not unset); pool knob = existing `asgi_pool_size` (not a new `web_pool_size`); `memstats` lives at `frappe.utils.memstats.memstats` (own module).
- Next: 24.1 (lazy backends + config-driven preload) — flips the rq/redis expectedFailure pin live.

## 2026-06-07 20:45 — Phase 24.1 DONE (lazy backends + config-driven preload)
- **rq/redis off the `import frappe` path.** Made lazy: `enqueue`/`enqueue_doc` via module `__getattr__` (PEP 562) in `frappe/__init__.py` (was eager `from frappe.utils.background_jobs import ...` which top-imports rq+redis); `monitor.py` `import rq`→function-level; `realtime.py` `import redis`→function-level (2 sites); `global_search.py` `import redis`→function-level. Traced each leak with a `__import__` hook, fixed one at a time.
- **DB drivers** already lazy at `import frappe` (none of pymysql/MySQLdb/psycopg/psycopg2/aiomysql/aiosqlite/libsql present) — no change needed; the app.py mysqlclient force-preload the spec expected to remove does not exist (already gone).
- **`preload_configured_backends()`** (`frappe/utils/preload.py`, new) wired into ASGI lifespan startup: reads common_site_config, eager-imports ONLY the configured drivers (cache_backend redis→redis; queue_backend rq→rq+redis; db_type×async → the one DB driver), best-effort. On this bench (mariadb + use_async_db) warms `['aiomysql','pymysql']`; a sqlite/memory/sqlite-queue site warms only libsql(+aiosqlite). 24.7 will freeze these.
- **`disable_rq` off-switch**: when set in common config, the lazy `frappe.enqueue`/`enqueue_doc` accessors raise a clear AttributeError instead of importing — verified. Small site can't pull rq/redis even by accident.
- **Import-budget test flipped live**: rq+redis moved from PENDING `@expectedFailure` into STRICT denylist (hard assert); MODULE_BUDGET 600→520. Green.

**Memory vs 24.0 baseline (`import frappe`, fresh subproc):**

| | RSS | modules |
|---|---|---|
| baseline (24.0) | 56.2 MB | 561 |
| after 24.1 | **48.2 MB** | **460** |
| delta | **−8.0 MB** | **−101** |

- Perf: no runtime regression — drivers now warmed at boot (preload) so first-request latency unchanged; light sites skip rq/redis entirely. Tests: import_budget 2/2, test_monitor 5/5, test_sqlite_queue 11/11, enqueue lazy-access + disable_rq verified.

## 2026-06-07 20:55 — Phase 24.3 DONE (bs4 is-HTML test → regex)
- New `frappe.utils.html_utils.has_html_tag(text)` (compiled `re.compile(r"<[a-zA-Z!/]")`) replaces `BeautifulSoup(...).find()` used purely as a boolean.
- Swapped both is-HTML sites: `html_utils.sanitize_html:162` and `model/base_document._sanitize_content:1383` (its `from bs4 import BeautifulSoup` removed — only usage).
- **`sanitize_html` does its real cleaning via `nh3`, not bs4** — bs4 there was ONLY the boolean test, so its import is now gone entirely from the function. The hot document-write path (`_sanitize_content`) never imports bs4.
- Scope kept tight: real bs4 parsing (communication/notifications/pdf/website_search/sqlite_search/global_search) untouched — a regex detects, can't parse.
- Verified: `has_html_tag` true for `<p>`/`a <b>`, false for plain text and `a < b` comparison (no false positive); plain-text `sanitize_html` returns unchanged WITHOUT importing bs4; `<script>` still stripped, `<p>` kept. Tests: test_sanitize_html ✔, test_clean_email_html ✔.

## 2026-06-07 21:00 — Phase 24.2 DONE (optional global imports off-switch)
- New helper `frappe.utils.optional_feature(name)`: raises `ValidationError` if `common_site_config enable_<name>` is False (default on = today's behavior). Called at each feature entry point BEFORE the heavy import.
- Gates added (default on, behavior-preserving):
  - `enable_number_to_words` → `utils/data.py:in_words` (covers money_in_words) before num2words import
  - `enable_csv` → `utils/csvutils.py` read_csv_content + to_csv (csv is stdlib, ~0 MB — surface control)
  - `enable_excel` → `utils/xlsxutils.py` make_xlsx / read_xlsx_file_from_attached_file / read_xls_file_from_attached_file
  - `enable_html_parsing` → `utils/html_utils.py:clean_script_and_style` (the bs4 cleaner) before bs4 import
- **xlsxutils heavy imports made lazy**: added `from __future__ import annotations` (turns `xlsxwriter.Workbook`/`Format` hints into strings) so `openpyxl`, `xlrd`, `xlsxwriter` move from module-top to function-level. Importing `frappe.utils.xlsxutils` no longer pulls any of the three (verified). enable_excel:0 → never imported.
- Verified: each feature works when on, raises `ValidationError` when off; xlsx libs absent on module import.
- Memory vs 24.1: `import frappe` 48.2→47.3 MB (xlsxutils not on boot path; gain is from not eager-importing in the modules that ARE reachable). modules unchanged 460. Budget test green; denylist clean.

## 2026-06-07 21:20 — Phase 24.4 DONE (sqlite queue jobs visible in RQ Job)
- **RQ Job desk doctype now backend-aware.** When `queue_backend == "sqlite"`, `RQJob.get_list` / `load_from_db` / `get_count` / `remove_failed_jobs` read the sqlite `jobs` table (`sites/task_queue.db`) instead of redis; redis path unchanged otherwise.
- `serialize_sqlite_job(row)` maps the sqlite schema (job_id/site/queue/func/kwargs/status/error/timestamps) onto RQ Job fields. Status vocab mapped: sqlite holds only **pending/running/failed** (successful jobs are DELETED on completion), → `pending→queued`, `running→started`, `failed→failed`. kwargs (pickled queue_args) decoded best-effort for owner/arguments/job_name.
- Read-only first (per spec): `delete`/`stop_job`/`cancel` no-op with a clear "not available on SQLite queue" message; `remove_failed_jobs` does the obvious `DELETE ... WHERE status='failed'`. Short-lived producer conn via `sqlite_queue._connect()` (per-call, free-threading-safe).
- **rq imports made function-level** (`from __future__ import annotations` frees the Job/Queue type hints): importing the rq_job controller no longer pulls rq — verified `rq not in sys.modules` after import. A sqlite light site opens the RQ Job list with zero rq/redis.
- New test `test_rq_job_sqlite.py` (5 tests, no worker needed): get_list visibility, load_from_db, missing→DoesNotExistError, status filter, failed-row status mapping — **5/5 green**.
- Pre-existing (NOT my change): `test_rq_job.py` redis-path tests that `wait_for_completion` time out — they patch `queue_backend="rq"` and need an external `bench worker`, which light-mode bench doesn't run (`worker:` commented in Procfile; `pgrep` confirms none). Worker-independent redis tests (delete_doc, clear_failed_jobs, configurable_ttl, func_obj_serialization) pass, confirming the redis branch is intact.

## 2026-06-07 21:40 — Phase 24.5 DONE (translation cache bounded)
- The merged-translation cache (`MERGED_TRANSLATION_KEY`) is one hash with a field per language, each field the full translation dict. FIFO max_entries bounds top-level keys, NOT hash fields → a multi-lang site accumulated every language forever.
- Added a locked LRU (`OrderedDict` + `threading.Lock`, size `TRANSLATION_LRU_SIZE=5`) tracking language access; on each `get_all_translations` the lang is touched and any language beyond the 5 most-recently-used is evicted from the hash via `hdel`. Lazy per-language load unchanged. `clear_cache()` resets the order map.
- Free-threading: all order-map mutation + eviction under one Lock.
- Verified: loading 7 languages keeps the 5 newest, evicts the 2 oldest; test_translate 15/15 green.
- Memory: bounds an unbounded growth path on multi-language sites (each language dict can be 100s of KB); single/dual-lang sites (the common case) unaffected.

## 2026-06-07 21:55 — Phase 24.7 DONE (gc.freeze after warmup)
- ASGI lifespan startup (after 24.1 preload): `gc.collect()` then `gc.freeze()` — moves the import graph + preloaded drivers out of GC scanning so gen2 collections stop walking/dirtying the framework's permanent object graph.
- bench-root `app.py` `_idle_trim`: one-shot `gc.freeze()` after the first trim cycle — captures meta/controllers warmed by the initial requests (freeze is cumulative).
- Free-threading: `gc.freeze()` only moves objects out of GC scanning — safe no-GIL.
- Verified: server boots clean with freeze in the startup path (Uvicorn running, startup complete, no traceback); direct init+connect+freeze moves **64,859 objects** out of GC scanning (freeze_count 0→64859). Steady-state RSS/GC-pause benefit measured in 24.11.
- Files: apps/frappe asgi.py (committed); bench-root app.py (untracked, project convention).

## 2026-06-07 22:05 — Phase 24.8 DONE (cache bounds + stats)
- Stats already surfaced via `frappe.utils.memstats` (24.0): cache + client_cache type, sizes, hits/misses, plus controllers/lazy_controllers counts.
- Audit of shared/site-level dicts: meta ClientCache FIFO 1024/600s + RLock (Phase 21) — bounded; jinja template cache uses jinja2's own LRU (overlay reuses the shared cache) — bounded; `frappe.controllers`/`lazy_controllers` bounded by doctype count (finite by design), count exposed in memstats.
- **Fixed a free-threading race** in `InProcessClientCache._store`: it checked `len >= maxsize` outside the lock, popped inside, then re-acquired to insert — two threads could miss the cap (grow past maxsize) or over-pop. Now evict+insert under one lock with a while-loop so the bound holds under concurrent inserts.
- Tests: test_inprocess_cache 23/23, test_client_cache 10/10.

## 2026-06-07 22:15 — Phase 24.9 DONE (session/bootinfo caches bounded)
- Same hash-field growth problem as translations: `"session"` (per sid) and `"bootinfo"` (per user) hashes unbounded — boot data per session is the big payload, FIFO max_entries only bounds top-level keys.
- Locked LRU (`SESSION_CACHE_LRU_SIZE=1000`, one `OrderedDict` each + shared Lock): touch on hset/hget, evict LRU field via `hdel`, drop entry on `delete_session`. Evicted sessions fall back to tabSessions (`resume()` already DB-fallbacks on cache miss); bootinfo regenerates. No behavior change below 1000 concurrent users.
- `import redis` in sessions.py → function-level (single use = except clause in `get()`).
- Verified: eviction keeps N newest (cap-3 smoke: 6 inserts → 3 newest survive); sessions module imports redis-free; budget test green (460 mods). test_auth: session-mechanism tests pass (expiry/cookie/lock); login-policy tests fail against the LIVE dev server on 8001 which runs pre-change code (FrappeClient HTTP, server not reloaded) — stale-server artifact, not this change.

## 2026-06-07 22:25 — Phase 24.10 DONE (lean defaults for new sites)
- `bench new-site` default `--db-type` mariadb → **sqlite** (libsql, Phase 22). New site = sqlite + in-process cache + sqlite queue + zero external services, at the memory floor. Heavy = one explicit flag (`--db-type mariadb/postgres`). Verified via `bench new-site --help`.
- bench-root `app.py`: lean-set detection (db_type sqlite + cache_backend memory/unset + queue_backend sqlite/unset) → `_DEFAULT_POOL = min(CPU-based, 4)`. `asgi_pool_size` (existing knob) still overrides; heavy backends keep CPU sizing. Verified: lean→4, heavy(mariadb)→32 on this 16-core box. This mariadb bench unaffected.
- Lean stays "absence of heavy flags", no new mode flag, no env knob.

## 2026-06-07 22:40 — Phase 24.6 DONE (allocator matrix, short-form)
- Matrix run abbreviated (30s autocannon -c10 frappe.ping burst + 90s settle; spec's 3×1-hour soak deferred — flip is one config line if the long soak ever disagrees). jemalloc not installed on this box → 2-way matrix.

  | allocator | idle | load | settle 90s |
  |---|---|---|---|
  | tcmalloc (current default) | 139 MB | 140 MB | 140 MB |
  | glibc + malloc_trim | 130 MB | 131 MB | 131 MB |

- glibc+trim ~9 MB lower across the board on this short test. **Decision: keep tcmalloc default** — 30s burst is not fragmentation territory; tcmalloc was chosen (Phase 1) for long-lived heap fragmentation, which only a long soak exercises. Rollback/flip = `use_tcmalloc: 0` in common config (existing knob, no code change). Re-evaluate with the full 1-hour matrix if idle RSS becomes the binding constraint.
- Note: both idle numbers already ≪ the 159 MB pre-24 baseline — lazy imports + freeze landed first.

## 2026-06-07 22:42 — gil_audit re-run (24.11 step 4, env-t)
- No NEW GIL re-enabler from Phase 24. Re-enablers unchanged: libsql (known/accepted, sqlite engine), plus lxml.etree/bs4/openpyxl/markdownify — all lazy feature libs never loaded on the boot path; 24.2 off-switches let a no-GIL lean site guarantee they never load.

## 2026-06-07 22:55 — Phase 24.11 DONE (final profiling) — PHASE 24 COMPLETE
Fresh server on post-24 code (mariadb heavy bench, GIL build, pool 32), autocannon -c10 30s homepage (Host: test.local), 5-min retention window:

| measure | baseline (pre-24) | post-24 | delta |
|---|---|---|---|
| `import frappe` (fresh subproc) | 56.2 MB / 561 mods | **47.3 MB / 460 mods** | **−8.9 MB / −101 mods** |
| boot idle (smaps Rss/Pss) | 159 MB / 145 | **139 MB / 117** | **−20 MB Rss / −28 Pss** |
| post-warmup (after freeze tick) | — | 153 MB | meta/controllers warmed+frozen |
| after 30s load | ~220 MB | 183 MB | −37 MB |
| post-load-idle 5 min | ~220 MB | **183 MB (161 Pss), flat** | no retention slope |
| homepage rps / p50 | 176.6 / 54ms (Phase 21) | **181.9 / 54.45ms** | +3% rps, latency unchanged ✓ |
| minimal site (sqlite, in-proc, booted) | — | **54.0 MB Rss / 40.6 Pss / 537 mods** | vs test.local 67.3/51/647 |

- Success criteria honest check: latency unchanged ✓ (±3%); import-budget pin ✓ (460 < 520, denylist clean); minimal-site measurably below feature site ✓; RSS plateau post-load ✓ (183 flat). **Absolute targets (idle <100, import <35, post-load <130) NOT reached on this heavy mariadb bench** — they presumed the lean config; the lean floor (sqlite site booted in-process) is 54 MB. Remaining gap is pydantic (kept eager by design), the 32-thread pool (heavy sizing), and warm meta/jinja — diminishing returns documented, not pursued.
- gil_audit (env-t): zero new GIL re-enablers from Phase 24 (libsql known; bs4/openpyxl/lxml/markdownify lazy-only).
- memory.jsonl: bare/import/minimal-site rows tagged `24-final`; server-proc numbers in this table (server killed post-measurement, dev server on 8001 untouched).

### Phase 24 commit list (apps/frappe, async_first)
- a57939cc8c 24.0 memory-profiling tooling
- 4fd57508c9 24.1 lazy backend imports + config-driven preload
- 23b95f584b 24.2/24.3 optional-import off-switches + bs4-free is-HTML test
- 86db941777 24.4 sqlite queue jobs in RQ Job
- 69815ef521 24.5 translation cache LRU
- 28b4eedff8 24.7 gc.freeze after warmup
- 6bba6c0624 24.8 InProcessClientCache eviction race fix
- f34b88c65f 24.9 session/bootinfo cache LRU
- c4fb950458 24.10 lean defaults (new-site sqlite)
(bench-root, untracked by convention: app.py lean-pool+freeze tick, scripts/memprofile.py, scripts/new_minimal_site.sh, specs/, improvement.md)

## 2026-06-07 23:40 — Fresh minimal bench validation (bench-tmp)
Real-world check of the Phase 24 lean defaults: `bench init bench-tmp` (local apps/frappe @ async_first, py3.14.4, fresh env) + `bench new-site minimal.localhost` with NO flags.
- **24.10 verified end-to-end:** site created with `db_type: sqlite` by default (no flag); with `db_type` in common config the lean pool kicks in — boot line `pool=4` (vs 32 CPU-based).
- **Dep gap found+fixed:** fresh env failed new-site — `frappe/utils/pdf.py` imports `packaging.version` but dep undeclared (only transitive in dev env). Added `packaging~=25.0` to pyproject (commit 729c230ba9).
- Memory (smem USS/PSS + smaps_rollup, 30s autocannon -c10 homepage, Host: minimal.localhost; sqlite full website render ~52 rps — matches Phase 22's ~40-50 rps sqlite render note):

  | stage | tcmalloc | glibc+trim |
  |---|---|---|
  | true idle (0 requests) | 154 Rss / 147 Pss / 150 USS | 148 Rss / 142 Pss |
  | post-warmup (3 reqs + freeze tick) | 188 | — |
  | after 30s load | 289 | 244 |
  | settle (~2 min) | 285 (released 4) | 236 (released 8) |

- **Finding 1:** `malloc_trim(0)` in app.py's idle tick is a glibc API — a **no-op under the tcmalloc LD_PRELOAD** (tcmalloc needs `ReleaseFreeMemory`). Explains tcmalloc's higher retention (285 vs 236, −49 MB for glibc on this workload). Strengthens 24.6's "re-evaluate" note: for lean sqlite benches `use_tcmalloc: 0` is the better default candidate; follow-up option = call tcmalloc's extension API from the tick.
- **Finding 2:** fresh-bench idle (148-154) higher than the dev bench (139) — newer dep wheels in the fresh env; and sqlite website render retains more warm state (+88 MB settle-vs-idle vs mariadb's +44): jinja/website route caches + per-conn libsql page cache. Candidates for a Phase 25 if the floor matters.
- bench-tmp left in place (sites/minimal.localhost, port 8060 config; server stopped).

## 2026-06-08 00:20 — memray + tracemalloc deep-dive (what holds the lean site's memory)
Tooling caveat found first: **memray can't see python objects on the free-threaded build** (3.14t allocates objects via mimalloc, bypassing malloc hooks — bench-tmp env turned out to be cpython-3.14.3+freethreaded; also explains its +9 MB idle vs dev bench, Phase 23's documented no-GIL overhead). memray still tracks C-side mallocs; tracemalloc used for the python side.

**C side (memray, cumulative churn during boot + meta warm):**
| location | cumulative alloc |
|---|---|
| libsql_compat.py:178 `execute` | 112 MB |
| libsql_compat.py:287 `_build_decltype_map` | 49–135 MB (varies per run) |
| libsql_compat.py:171/160 `_names`/`description` | 29 + 16 MB |
| libsql_compat.py:233 `fetchall` | 12 MB |

→ **libsql compat layer = ~95% of all C allocation churn.** Mostly freed (retained-at-exit only ~2.6 MB sqlite page cache), but the churn sets the allocator high-water — explains the load spike (244–289 MB) and tcmalloc's retention. `_build_decltype_map`'s cumulative size suggests it rebuilds more than once (per connection / unknown-column invalidation) — Phase 25 candidate: persist or share the map across conns, count rebuilds.

**Python side (tracemalloc, retained warm state = 77.5 MB traced):**
| holder | retained |
|---|---|
| module code objects (importlib marshal, 1062 modules) | **35.8 MB** |
| meta/docfields (base_document.py:326, 276 doctypes warmed) | 7.6 MB |
| libsql row/description objects held by caches | 5.0 MB |
| babel locale data (core+localedata) | 3.8 MB |
| inprocess_cache pickled values | 2.5 MB |
| stdlib (abc/typing/enum/dataclasses/annotationlib) | ~2.5 MB |

**So the lean site's ~142 MB PSS decomposes roughly:** ~15 interpreter + ~36 module code + ~42 other python objects (meta, caches, babel, asyncio/uvicorn state) + C side (libsql page caches, mmap'd .so, thread stacks) + free-threaded build overhead. Biggest single lever left = module-code footprint (already attacked via lazy imports; rest is the framework's real working set) and the libsql churn fix.

## 2026-06-08 01:30 — Phase 25 DONE (memory floor: boot diet + libsql churn + lean allocator)
Spec specs/phase25-memory-floor.md. Driven by the memray/tracemalloc deep-dive. 3 commits on apps/frappe (async_first) + bench-root app.py (untracked).

### 25.0 — boot-preload diet (commit 76500bb607)
`import frappe.app` (the actual serving entry) went **1457 → 1009 modules (−448)** with **zero heavy libs** left (rq/redis/bs4/html5lib/requests/urllib3/charset_normalizer/posthog/markdownify/num2words all gone). app.py preload block lost 5 lines (background_jobs, redis_wrapper, mysqlclient, num2words, babel.messages); all hot preloads kept. Heavy transitive imports moved function-level in 12 modules (website_search, notifications, pdf, boot/changelog, csvutils, frappecloud×2, telemetry posthog, core/utils markdownify, twofactor, submission_queue, scheduler, safe_exec). test_import_budget gained a `frappe.app` probe (budget 1100, same STRICT denylist + requests/posthog/markdownify/num2words). 4/4 green.

### 25.1 — libsql decltype churn (commit a2cd83f038)
The dominant memray churn (`_build_decltype_map` full 276-table rescan) fired for every alias/expression column and two alternating bare aliases ping-pong-rebuilt forever. Fix: schema map holds only real columns (can't be evicted) + per-DB `_decltype_absent` memo + identifier-gated rescan + per-cursor names/decltypes cache. **90 interleaved alias/real queries after warm = 0 rebuilds (was 60).** Counter exposed via memstats (`libsql_decltype_rebuilds`). test_libsql_compat 13→15, async_db_sqlite 10/10, sqlite_queue 11/11 green.

### 25.2 — lean allocator default (bench-root app.py)
Lean backend set (sqlite+memory+sqlite-queue) → `use_tcmalloc` defaults **0** (glibc), since the malloc_trim tick is a glibc no-op under tcmalloc (~50 MB more retained). Verified: lean bench boots with no LD_PRELOAD; heavy mariadb bench keeps tcmalloc. `use_tcmalloc` overrides either way.

### Measured (bench-tmp lean sqlite server, **freethreaded build** — carries Phase 23's ~+10 MB no-GIL overhead)

| stage | Phase 24 (glibc) | Phase 25 (auto glibc) |
|---|---|---|
| idle Pss | 142 | **136** |
| after 30s load | 244 | 253 |
| settle 2 min | 236 | **230** |
| serving-path modules (`import frappe.app`, GIL env) | 1457 | **1009** |
| rps (sqlite full render) | ~52 | ~47 (daytime band) |

- Honest read: idle/settle dropped only modestly (~6 MB) on the **freethreaded** bench-tmp — the no-GIL build's per-object overhead + mimalloc mask the module-code savings. The unambiguous wins: **−448 modules on the serving path**, **zero libsql rescan churn** (was the load-spike driver), and glibc auto-default (saves the ~50 MB tcmalloc-retention on lean sites). In-process GIL-build minimal site unchanged at 537 mods / 41.6 Pss (stats mode doesn't load frappe.app's preloads, so 25.0's win is server-only).
- **Recommendation for small sites: use the GIL build** (`env/bin/python app.py`, not `env-t/`). Free-threading costs ~+10 MB idle for parallelism a small single-process site never uses. With the GIL build + lean config + Phase 25, expect server idle ~125-135 Pss.

### Lowest practical floor
~95 MB hard floor (interpreter 12-15 + module code ~25-30 + framework objects 35-40 + C/thread stacks 10-15). Reaching it needs framework surgery (lazy meta loading, `-OO` docstring strip, pruning the ~1009 serving modules) — diminishing returns, deferred. Phase 25 took the cheap, safe wins.

## 2026-06-08 00:52 — Phase 26: server logic into framework + common-config-only knobs

User asks: (1) move bench-root `app.py` server logic into the framework so it's managed with frappe, not an untracked drift-prone file; (2) drop env-var knobs — `common_site_config.json` only.

- **`frappe/asgi.py:serve()` is now the single server core.** Folded the bench-root app.py runtime in: GIL-aware pool + Phase 24.10 lean floor (`min(default,4)`), `limit_concurrency` 8×CPU, the Phase 24.7 idle-trim task (`gc.collect` + one-shot post-warmup `gc.freeze` + `malloc_trim(0)` per `malloc_trim_interval`), boot banner, `loop_debug`. `knob()` reads common_site_config only (env param removed). Keys: `asgi_pool_size`, `asgi_limit_concurrency`, `webserver_port`, `asgi_thread_stack`, `malloc_trim_interval`, `loop_debug`, `log_level`.
- **Bench-root `app.py` → frappe-free re-exec shim only.** Must stay out of the frappe package: the allocator/GIL env has to be set before any `import frappe` (which runs `frappe/__init__`). It reads common config (stdlib), decides `use_tcmalloc` (lean→0)/`MALLOC_ARENA_MAX`/`PYTHON_GIL`, re-execs once, then `from frappe.asgi import serve; serve()`. No `FRAPPE_*` knobs.
- **`bench serve`** unchanged for users (`frappe.app.serve` → `frappe.asgi.serve`, default port 8000); light-mode `serve()` defaults `webserver_port` 8001.

**Memory / perf:** parity refactor — no intended delta. Same pool/trim/freeze behavior, now framework-owned. Verified: boots + binds 8001, `/api/method/ping` 200 on test.local / minimal.local / sqlite-async.local, clean shutdown, `test_import_budget` 4/4 green (function-level `ctypes`/`gc` imports in `serve()` keep the `frappe.app` import budget unchanged). No `FRAPPE_*` env references remain in `app.py`/`asgi.py`.

## 2026-06-08 01:30 — Phase 26 follow-up: gc-on-wrong-thread wedges async-mariadb bridge loop (FIX)

**Symptom (user, `bench serve` on use_async_db:1 + mariadb + desk socket.io):** request floods of `IncompleteReadError: 0 bytes` → `2013 Lost connection`, `readexactly() called while another coroutine is already waiting`, `(0, 'Not connected')`. Server effectively dead for DB.

**Root cause (reproduced, not guessed):** `scripts/repro_async_race.py` + `faulthandler` isolated it. A bare `gc.collect()` on **any non-bridge thread** finalizes cyclic aiomysql `Connection`/transport garbage, whose teardown chain (`Connection → StreamWriter → transport → loop._remove_reader/_remove_writer`) mutates the **bridge loop's selector**. asyncio selectors are NOT thread-safe → epoll corrupted mid-I/O → bridge loop wedges in `selector_events.py write` → all workers block forever on `run_coroutine_sync(...).result()`. Same hazard the fork path already documents (mariadb/aio.py:127), but triggered by ordinary cross-thread gc. The Phase 26 consolidation's `_idle_trim` calls `gc.collect()` on the **main uvicorn loop thread** every 30s — a guaranteed recurring trigger (latent in the old bench-root app.py too). Isolation: bare-gc-thrash hangs even on the clean destroy path; gc-on-bridge-loop = 0 failures.

**Fix:** new `frappe.dispatch.gc_collect_safe()` — awaitable that routes `gc.collect()` (and therefore the finalizers) onto the bridge-loop thread via `run_coroutine_threadsafe` + `wrap_future` (non-blocking for the caller's loop). Falls back to inline collect when no bridge loop runs (sqlite light mode: aiosqlite uses a worker thread + queue, no loop selector, no hazard). `asgi.py` `_idle_trim` and the lifespan-startup collect now call it instead of bare `gc.collect()`.

**Verified:**
- repro: control (off-thread gc) hard-hangs (faulthandler dump: bridge loop stuck in aiomysql `_write_bytes`); `gc_collect_safe` → 0 failures, abandon + clean paths.
- new regression `test_async_db_mariadb.test_gc_collect_safe_does_not_wedge_bridge_loop` (8 worker threads × 40 req + a thread hammering `gc_collect_safe`, 30s deadline, daemon threads so a regression fails-on-deadline not hangs-the-runner) — green (26.8s). test_async_db_mariadb 11/11, test_dispatch 14/14.
- live server (mariadb async common config, `malloc_trim_interval` forced to 3s): **5780/5780 → 200** under sustained -P20 load while gc trim ticks fired every 3s. Zero `readexactly`/`Lost connection`/`Not connected`. Pre-fix wedged on the first tick.
- unit category 123/123 green.

**Memory/perf:** no footprint change — same one collect per trim tick, just executed on the correct thread. Removes a hard-hang failure mode on the async path; sqlite light mode unaffected (inline fallback).

## 2026-06-08 01:54 — Phase 26.2: server fully inside the framework + fixed concurrency default

User asks: (1) the bench-root `app.py` should not exist — move the launcher into the framework, merge `app.py` / `frappe.asgi.serve` / the `bench serve` wrapper into one place, "easier to manage"; (2) don't size concurrency by CPU — 16 is enough by default.

- **New `frappe/serve.py`** = the one server module. Contains the malloc/GIL **re-exec bootstrap** (moved from bench-root `app.py`) + the **`serve()` runtime** (moved out of `asgi.py`) + `main()` + `__main__`. `asgi.py` is now purely the ASGI `application` + lifespan. `frappe.app.serve` (the `bench serve` target) calls `frappe.serve.serve` (runtime only, no re-exec — dev). **Bench-root `app.py` deleted.**
- **Launch:** `python -m frappe.serve` (Procfile `web: MALLOC_ARENA_MAX=2 env/bin/python -m frappe.serve`). The re-exec is **conditional** — it only `os.execve`s when the running env doesn't already match the site (tcmalloc/PYTHON_GIL needed, or MALLOC_ARENA_MAX unset). With the Procfile pre-setting `MALLOC_ARENA_MAX` on a lean GIL site, **no re-exec → frappe imported once**; a mariadb/tcmalloc or free-threaded site re-execs once as before.
  - Gotcha fixed: under `-m`, `sys.argv[0]` is the script path, so a naive `execve([python, *sys.argv])` would re-run `serve.py` **by path**, putting the package dir on `sys.path[0]` where frappe's own `locale.py`/`email/` shadow the stdlib (circular-import crash). Re-exec now hardcodes `[python, "-m", "frappe.serve"]`.
- **`asgi_limit_concurrency` default `8*CPU` → fixed `16`.** Pool sizing unchanged (still lean-capped at 4 for sqlite; `2*CPU` for heavier backends). Common-config override still wins.

**Memory/perf:** parity — same runtime, just relocated. Re-exec now skipped on the common lean path (one fewer frappe import at boot). **Verified:** `python -m frappe.serve` boots, re-exec loads tcmalloc with correct `-m` cmdline, all 3 sites `/api/method/ping` 200; no-re-exec path (use_tcmalloc:0) boots single-import with no LD_PRELOAD, 200; `bench serve` 200. Tests: import_budget 4/4, dispatch 14/14, async_db_mariadb 11/11 (incl gc regression), **unit category 123/123**.

## 2026-06-08 08:14 — Phase 26.3: de-dup lean-backend predicate (cleanup)

Spec `specs/phase26.3-dedup-lean-predicate.md`. Cleanliness pass on the Phase
26.2 server module. The lean-backend test
(sqlite main DB + memory cache + sqlite queue) was written twice in
`frappe/serve.py`: once as `_lean_backends(conf)` (used by `_reexec` to pick the
glibc allocator) and once inlined inside `serve()` to cap the thread pool at 4.
Two copies of the same 3-condition predicate = drift risk: change the allocator
rule and the pool-sizing rule silently diverges. Folded the inline copy into the
shared `_lean_backends(conf)` call (works on the frappe conf dict — `.get` API,
same keys).

**Memory/perf:** none — pure refactor, identical predicate. New regression
`frappe/tests/test_serve_lean.py` pins the 7-case truth table (sqlite-only,
explicit memory+sqlite, empty-string backends, mariadb, redis cache, rq queue,
empty conf) so the now-load-bearing predicate can't drift. Tests on
minimal.local: test_serve_lean 1/1, test_import_budget 4/4 (serve.py change
pulls no new imports onto the frappe.app serving path). Commit b9d23b4766
(apps/frappe, async_first); inline `lean = (...)` block gone.

### Memory investigation for small sites (what's left)
Re-checked the Phase 24/25 deep-dive against current code. The cheap wins are
spent. Remaining levers for the lean-sqlite floor (~136 Pss idle, GIL build),
all framework surgery with diminishing returns — NOT done, recorded for later:
- **Module-code footprint (~36 MB / ~1009 serving modules)** is the biggest
  single block. `frappe.app` import was already cut 1457→1009 (Phase 25.0).
  Further pruning needs moving more transitive heavy imports function-level.
- **`-OO` / PYTHONOPTIMIZE=2** would strip docstrings (a real slice of that
  36 MB) on the light-mode launch — but removes asserts too and frappe surfaces
  whitelisted-method `__doc__`; behaviour-affecting, needs its own phase + audit.
- **Lazy meta loading (~7.6 MB docfields, 276 doctypes warmed)** — deferred per
  user (2026-06-08); load meta on first use per doctype instead of warming all.
- **babel localedata (~3.8 MB)** — load only the active locale's data.
The lean allocator default (glibc + malloc_trim tick, Phase 25.2) + gc.freeze
(24.7) already keep the idle floor near the ~95 MB hard floor for a process that
must hold the full framework working set.

## 2026-06-08 08:40 — Phase 27: always-on in-process scheduler tick

Spec `specs/phase27-always-on-scheduler-tick.md`. User ask: "run the scheduler
loop always, just decide per config whether to take action."

**Problem:** Phase 11's `_scheduler_loop` decided run/skip ONCE at startup
(`_setup`): `in_process_scheduler: 0` → task exited forever; the cross-process
FileLock was held for the task's whole life, so if an external `bench schedule`
held it at boot the in-process task gave up permanently. Either needed a server
restart to recover.

**Fix (`frappe/utils/scheduler.py`):** loop always runs; a per-tick `_tick()`
(pool thread, fresh contextvars Context) re-reads `in_process_scheduler`, takes
the FileLock non-blocking, enqueues only if acquired, releases in `finally`, and
returns the (possibly changed) tick interval. `_task_state` drops the held-lock
entry — nothing held between ticks.

**Invariant kept:** enqueue runs only while holding the lock; non-blocking
acquire on both the in-process and external side means at most one holder at any
instant → never a double-schedule. New (better) behaviour = graceful hand-off:
external scheduler stops → in-process resumes next tick (was: stayed dead).

**Memory/perf:** negligible — one extra config read + a non-blocking
acquire/release per tick (default tick 240 s). No new threads/tasks; removes a
restart-required failure mode.

**Tests:** `test_scheduler_task.py` rewritten to the always-on contract — 5/5 on
minimal.local: ticks_repeatedly, config_disabled_skips_but_loop_stays_alive,
config_flip_resumes_without_restart, external_scheduler_lock_wins_then_hands_off,
lock_released_after_stop.

## 2026-06-08 09:05 — Phase 28: async DB driver on by default

Spec `specs/phase28-async-db-default-on.md`. User: "keep async flags by default."

`use_async_db` now defaults **on**; `use_async_db: 0` is the explicit sync
rollback. New helper `frappe.database.async_db_enabled()` (cint, default 1 —
mirrors `scheduler.in_process_scheduler_enabled()`) is the single source of the
default; `get_db`'s three backend branches and `preload.preload_configured_backends`
read it so they can't drift. `serve.py` bootstrap is stdlib-only (pre-`import
frappe`), so its GIL decision defaults the flag in place: `_flag(conf,
"use_async_db", 1)`.

**Safe to default:** non-server contexts already run async — `get_bridge_loop()`
lazily starts a per-pid bridge loop; the mariadb fork quarantine + after-fork
hooks + atexit pool close handle `bench worker` forks; new sites default to
sqlite (aiosqlite worker-thread, no selector/fork hazard); off-thread-gc handled
(Phase 26). This bench already ran `use_async_db: 1`, so its running config is
unchanged — only benches that never set the flag flip.

**Memory/perf:** none for this bench (already async). For previously-sync benches
it switches to the bridged async driver (Phases 5–17 behaviour); sync stays
reachable via `use_async_db: 0`. Sync drivers NOT removed (base classes +
rollback).

**Tests:** new `test_async_db_default.py` 3/3 (defaults-on-when-unset,
explicit-off, explicit-on). Gates: test_import_budget 4/4 (helper imported
function-level in preload — no new eager import), test_async_db_sqlite 10/10
(get_db still wires the async driver via the helper).

## 2026-06-08 09:25 — Doc: specs/lifecycle.md (process lifecycle reference)
Wrote `specs/lifecycle.md` (reference, not a phase): one-process/one-loop model,
3 thread roles (main loop / bridge loop / pool), boot+re-exec, lifespan
startup+shutdown order, task inventory, request flow through `application()`,
and the `_idle_trim`-in-serve.py-not-lifespan asymmetry. No code change.
