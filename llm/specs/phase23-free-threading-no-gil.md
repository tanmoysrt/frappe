# Phase 23: Free-threaded Python — disable the GIL

**Part:** Cleanup / Performance
**Depends on:** Phase 18. Strongly prefer after Phase 20 (smaller C-extension surface once Werkzeug/gunicorn gone) and Phase 22 (libsql wheel must be picked with cp314t in mind).
**Usable after:** yes — ships as an alternate interpreter; GIL build remains the fallback until proven.

## Goal

Run the single-process stack on free-threaded CPython (PEP 703/779,
officially supported since 3.14). Today the ThreadPoolExecutor running all
sync frappe work is GIL-serialized — one core of Python execution no matter
the pool size. No-GIL makes pool threads truly parallel: the single process
finally scales across cores without gunicorn workers.

## Why this matters here specifically

The whole migration bet on one process + thread pool. With the GIL, that
caps CPU-bound throughput at ~1 core (current ~120 rps is mostly I/O overlap).
Free-threading is the missing half of the single-process story: N pool
threads ≈ N cores, with the memory model we already built (per-request
contextvars dict, per-Database OwnedLock serialization).

## Analysis required FIRST (deliverable: analysis.md section, no code)

1. **Interpreter availability.** Current env: 3.14.4 GIL build
   (`Py_GIL_DISABLED: 0`). Fedora 44: check `python3.14-freethreading` pkg;
   else build CPython `--disable-gil` or use uv-managed `cpython-3.14t`.
   New venv `env-t/` beside `env/` — never replace the working one.
2. **C-extension wheel audit (the gate).** For every pyproject dep, check
   cp314t wheel availability + `Py_mod_gil` declaration. Extensions that don't
   declare support force the GIL back ON at import (runtime prints a warning;
   `PYTHON_GIL=0` overrides at own risk). Known C/Rust extensions in our tree:
   `mysqlclient`, `hiredis`, `orjson`, `Pillow`, `cryptography`, `MarkupSafe`,
   `PyYAML(libyaml)`, `nh3`, `pydantic-core`, `psycopg2-binary`, `psutil`,
   `charset-normalizer`, `websockets`, `aiomysql→PyMySQL(pure, fine)`.
   Output: table dep → cp314t wheel? → declares free-threading? → fallback
   (pure-python mode / pin newer / drop dep). `psycopg2-binary` likely the
   worst offender → swap to `psycopg[binary]` if postgres support must stay.
3. **Thread-safety audit of OUR code under true parallelism.** GIL-build bugs
   hide behind bytecode-level atomicity; no-GIL exposes them:
   - module-level mutable caches: `ClientCache` (redis_wrapper.py:862) dict +
     FIFO eviction; `frappe.local` ContextVar dict (per-request, fine);
     controller/module import caches in frappe.__init__; `_state` dicts in
     realtime_server/dispatch; sqlite_queue producer conns.
   - `OwnedLock` already correct (real threading.Lock underneath).
   - `sqlite3`/libsql connections: one-thread-per-conn already enforced.
   - dict/list racing: 3.14t makes builtin ops thread-safe per-op, but
     check-then-act sequences (`if key not in cache: cache[key] = build()`)
     need locks or "last write wins is fine" annotation. Audit + annotate each.
4. **Cost model.** Free-threaded build: ~5–10% single-thread slowdown +
   higher per-object memory (immortalization, biased refcounting). Measure
   both before committing: same benchmark, GIL vs no-GIL, 1 vs N concurrency.

## Tasks (after analysis green-lights)

- Build `env-t/` venv on cpython 3.14t; install deps per audit table.
- Fix audit findings: add locks where check-then-act matters, swap deps
  without cp314t wheels.
- `app.py`: detect free-threaded build (`sys._is_gil_enabled()`), log status
  at boot; pool size default becomes `min(2×CPU, FRAPPE_POOL_SIZE)` — revisit
  sizing, GIL-era 2×CPU was an I/O-overlap number, no-GIL wants ≈CPU for
  CPU-bound headroom.
- Procfile alt line: `web: env-t/bin/python app.py` (commented swap, like the
  Phase 1 rollback pattern).
- Stress test: autocannon -c50 sustained 5 min, assert zero 5xx, RSS plateau,
  and `PYTHON_GIL` warnings absent from logs.

## Verification

- Full unit + targeted integration on env-t, both backends.
- test_aio_orm + test_dispatch under -c high concurrency (the lock paths).
- Benchmark table in improvement.md: GIL vs no-GIL rps/p50/RSS at -c10 and
  -c50. Success = no-GIL wins at -c50 without losing >10% at -c10.

## Rollback

Procfile line back to `env/bin/python app.py`. Both venvs coexist; zero code
paths depend on the free-threaded build.
