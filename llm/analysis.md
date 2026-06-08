# Cleanup Phase Analysis

## Phase 23: Free-threaded Python

Current interpreter:

- `env/bin/python`: CPython 3.14.4, GIL build.
- `Py_GIL_DISABLED`: `0`.
- `sys._is_gil_enabled()`: `True`.

Decision:

- Do not replace the working `env/` interpreter.
- Keep the free-threaded runtime as a separate `env-t/` validation path.
- `app.py` now logs GIL status at boot so both builds are observable.

Dependency gate:

- `libsql` is not installed in the current interpreter, so Phase 22 cannot be flipped to libsql by default yet.
- The current tree still contains C/Rust extensions that need cp314t wheel verification before `PYTHON_GIL=0` can be considered safe: `mysqlclient`, `hiredis`, `orjson`, `Pillow`, `cryptography`, `MarkupSafe`, `PyYAML`, `nh3`, `pydantic-core`, `psycopg2-binary`, `psutil`, `charset-normalizer`, and `websockets`.
- `psycopg2-binary` remains a likely blocker; prefer `psycopg[binary]` if Postgres support needs to run under no-GIL.

Thread-safety notes:

- The new in-process cache uses an `RLock` around all shared mutable maps/lists/sets; this is required for free-threaded Python.
- Existing async DB connection ownership still funnels each request connection through its per-Database lock.
- Remaining module-level caches need a follow-up audit before enabling `env-t/`: controller/module caches in `frappe.__init__`, realtime server `_state`, dispatch bridge state, and any check-then-act cache population paths.

Next gate:

- Build `env-t/` with a CPython 3.14t interpreter.
- Install dependencies and reject any extension that re-enables the GIL at import.
- Run the same benchmark matrix under GIL and no-GIL before changing Procfile defaults.

## Phase 22 analysis — libsql replacing sqlite3/aiosqlite (2026-06-07)

Binding probed: PyPI `libsql` 0.1.11 (Rust/PyO3, ex libsql-experimental;
cp314 manylinux wheel AVAILABLE; no cp314t wheel; `libsql-experimental`
is sdist-only → Rust build).

Feature table vs our actual usage:

| Need | Site | libsql 0.1.11 | Verdict |
|---|---|---|---|
| `UPDATE…RETURNING` | sqlite_queue atomic claim | works | OK |
| executemany / executescript | backend, queue | works | OK |
| FTS5 | sqlite_search | works | OK |
| busy_timeout PRAGMA | queue | accepted | OK |
| `create_function` (REGEXP, regexp_replace) | sqlite/aio.py — ORM REGEXP queries | **MISSING** | **BLOCKER** |
| `register_converter` + PARSE_DECLTYPES (timestamp/date/time) | sqlite backend type round-trip | **MISSING** | **BLOCKER** |
| `row_factory` / sqlite3.Row by-name | queue producer, backend dict rows | **MISSING** (plain tuples) | blocker (adapter could wrap) |
| DB-API exception hierarchy (IntegrityError etc.) | queue dedup, database.py error mapping | **MISSING** — raises bare `ValueError` | **BLOCKER** (string matching only) |
| WAL on a live file shared with stdlib sqlite3 | task_queue.db: CLI producer + server worker | **BROKEN** — `PRAGMA journal_mode=WAL` reports `wal` but writes a `-journal` file; stdlib conn on the same path cannot see committed tables | **BLOCKER** |
| `total_changes`, `backup`/`iterdump`, `interrupt` | bench backup paths, misc | missing | workaround possible (file copy) |
| embedded replica (`sync_url` + `conn.sync()`) | DR headline | present | OK (the one win) |

Verdict: **implementation gated — do not flip any backend.** The compat
adapter cannot emulate user-defined SQL functions, type converters, the
exception hierarchy, or live-file WAL interop; flipping would break ORM
REGEXP filters, datetime round-trips, queue dedup error handling and the
CLI-producer/server-worker shared queue file. `sqlite_engine: "stdlib"`
stays the default (knob reserved); re-evaluate when the binding ships:
create_function, sqlite3-compatible exceptions, row_factory, and honest
pass-through WAL. Phase 23 interplay: no cp314t wheel either, so nothing
is lost by waiting.

## Phase 23 analysis — free-threaded CPython (2026-06-07)

Interpreter: no Fedora 44 pkg; uv-managed `cpython-3.14.3+freethreaded`
installed, `env-t/` venv created (sys._is_gil_enabled() == False).
Procfile already carries the commented `web-t:` swap line.

cp314t wheel/build audit (all deps attempted into env-t):

| Dep | cp314t status | GIL declaration | Fallback |
|---|---|---|---|
| **orjson 3.11.9** | **NO wheel; source build REFUSES**: "orjson does not support free-threaded Python" (maturin/pyo3 gate) | n/a | **HARD BLOCKER** — frappe imports orjson unconditionally in core modules; stdlib-json shim = invasive + hot-path regression |
| psycopg2-binary | no wheel; source build needs pg_config | n/a | drop (postgres unused here) or swap to `psycopg[binary]` |
| mysqlclient (MySQLdb) | wheel builds/imports | **re-enables GIL** at import | avoidable: light mode uses pymysql/aiomysql (`use_mysqlclient: 0`, `use_async_db: 1`); app.py preloads frappe.database.mariadb.mysqlclient → must become lazy on env-t |
| hiredis | imports | **re-enables GIL** | omit extra — redis-py runs pure-python parser; light mode has no redis anyway |
| lxml | imports | **re-enables GIL** | pulled by premailer/cssutils/html5lib/bs4; premailer imports lazily in email_body.py — GIL would re-enable the first time an email is inlined |
| Pillow, cryptography, MarkupSafe, PyYAML, nh3, pydantic-core, psutil, charset-normalizer, websockets, pymysql, aiomysql, aiosqlite, uvicorn | wheels fine | gil_off (declared safe) | OK |

Everything else (~70 deps) installed clean.

Thread-safety audit of our code (prep already in place):
- per-Database `OwnedLock` — real threading.Lock, correct without GIL
- InProcessCache (Phase 21) — designed with one RLock for exactly this
- frappe.local — per-context ContextVar dict, no cross-thread sharing
- remaining check-then-act candidates when unblocked: controller/module
  import caches in frappe.__init__, realtime_server/dispatch `_state`
  dicts, sqlite_queue producer conns (one-per-thread already)

Verdict: **GATED on orjson upstream free-threading support.** Without an
installable orjson the framework cannot import on 3.14t, so the
benchmark cost-model (GIL vs no-GIL rps/RSS) cannot run. Re-evaluate on
each orjson release (`uv pip install --python env-t/bin/python orjson`);
when it lands: make the mysqlclient preload lazy, exclude hiredis,
accept (or lazy-load) the lxml email path, rerun the wheel audit, then
proceed with the spec's pool-resize + stress steps. env-t/ kept beside
env/ for that day.

## Phase 22 addendum (2026-06-07, later) — gate LIFTED, implemented

User decision: main DB on libsql for concurrency. Re-probing overturned
two "blockers" from the first pass:
- WAL live-file interop: WORKS (first test corrupted itself with
  autocommit=True + PRAGMA inside a transaction). Concurrent libsql +
  stdlib writers on one WAL file: zero errors.
- create_function/REGEXP: libsql ships REGEXP NATIVELY — strictly better
  than the stdlib backend's python-callback (which serializes on the
  interpreter).

Remaining gaps absorbed in `frappe/database/sqlite/libsql_compat.py`
(exception mapping onto stdlib sqlite3 classes, Row, decltype-map type
conversion cached per DB file, datetime param adaption, statement
draining, aiosqlite-shaped async runner). Main DB = libsql only; queue +
search intentionally stay on stdlib sqlite3 (independent files);
mariadb/postgres routing untouched. Full parity matrix in
improvement.md. Lesson recorded: the original WAL/interop verdict came
from a contaminated experiment — re-verify blockers in isolation before
gating.

## Phase 23 addendum (2026-06-07, later) — gate LIFTED, implemented

orjson blocker dissolved by swapping the whole framework to **msgspec**
(declares free-threaded support; same strict JSON; ~5x stdlib). No
compat shim — call sites use `msgspec.json` directly; `orjson_dumps`
keeps its name but the `default`-hook path goes through stdlib json so
`json_handler` still controls datetime format.

Re-ran the cp314t wheel audit with `scripts/gil_audit.py` (per-module
subprocess probe of `sys._is_gil_enabled()` post-import). Transitive
re-enablers fixed: mysqlclient 2.2.8, lxml/premailer optional (email
style-inlining degrades gracefully), hiredis omitted, psycopg2-binary →
`postgres` extra. **libsql is the only GIL re-enabler left** (sdist
wheel, no Py_mod_gil) — accepted on a sqlite bench (or PYTHON_GIL=0).

Free-threading is opt-in (`free_threading` common-config knob) and gated
on `use_async_db` — app.py forces PYTHON_GIL on/off in the pre-import
re-exec. Async postgres added on **psycopg3** (aiopg's commit() raises
in async mode; psycopg.AsyncConnection has real async txns) —
structurally complete, unverified against a live PG.

Benchmarks (16-core, cpython-3.14.3t): CPU-bound ~10x (49→483 rps),
full-dispatch ping ~4.4x (815→3622 rps); no-GIL wins at both -c10 and
-c50. RSS +12% idle. Soak -c50 clean (0 5xx, 0 GIL warnings) after
re-basing limit_concurrency on CPU (the smaller no-GIL pool had halved
it → spurious 503s). Spec success criteria met.
