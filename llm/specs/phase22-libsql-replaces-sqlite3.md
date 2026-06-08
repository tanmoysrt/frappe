# Phase 22: Replace sqlite3/aiosqlite with libsql

**Part:** Cleanup / Storage upgrade
**Depends on:** Phase 18. Coordinate with Phase 23 (cp314t wheel availability is a selection criterion) and Phase 21 (sqlite-backed cache bump-file unaffected).
**Usable after:** yes — backend-by-backend flip, stdlib sqlite3 stays the fallback until parity proven.

## Goal

All SQLite-family storage runs on libsql (the SQLite fork): the sqlite DB
backend, the Phase 9 task queue, and sqlite_search. Buys: embedded replicas
(live sync of a site DB to a remote — backup/DR story for light mode),
`sqld` server mode as a future scale-out path, encryption at rest, and an
actively-developed engine while keeping the SQLite file format.

## Current state (audited 2026-06-07)

stdlib `sqlite3` imports:
- `frappe/database/sqlite/database.py` — sync sqlite backend
- `frappe/database/sqlite/aio.py` — AsyncSQLiteDatabase (aiosqlite, Phase 5)
- `frappe/utils/sqlite_queue.py` — sync producers (sqlite3) + async workers (aiosqlite), `sites/task_queue.db`
- `frappe/search/sqlite_search.py` (+ test) — FTS5
- `frappe/database/database.py`, `frappe/_optimizations.py`,
  `frappe/concurrency_limiter.py`, `frappe/commands/test_commands.py` — small/incidental uses (audit each)

aiosqlite is itself just sqlite3-in-a-thread — there is no asyncio in the
engine; our BridgedConnection pattern (Phase 5) already owns the threading.

## Analysis required FIRST

1. **Binding selection.** PyPI `libsql` (Rust binding, ex `libsql-experimental`)
   exposes a sqlite3-like DB-API. Verify against our actual usage:
   `row_factory`/Row access by name, `executescript`, `executemany`,
   `create_function` (used? grep), `total_changes`, `interrupt`,
   `iterdump`/`backup` (bench backup paths!), PRAGMA passthrough
   (`cache_size`, `mmap_size`, `busy_timeout`, WAL), `RETURNING` support
   (sqlite_queue's atomic claim depends on `UPDATE…RETURNING`).
   Output: feature table; any gap = blocker list with workaround.
2. **Wheel audit.** cp314 (and cp314t for Phase 23) wheels on linux x86_64;
   build-from-source cost if absent (Rust toolchain).
3. **Async story.** libsql binding is sync → wrap with our existing pattern:
   either (a) port `aiosqlite`'s thread-runner around a libsql connection
   (small shim, aiosqlite's Connection is ~300 lines), or (b) run libsql conns
   directly on the bridge loop via `sync_to_async` per op. Pick by benchmark;
   (a) preserves Phase 5's shape exactly.
4. **File-format compatibility.** libsql reads/writes standard SQLite files —
   verify both directions with our actual site DBs (open existing
   sqlite-async.local db with libsql, run integration; reopen with stdlib
   sqlite3 after). Embedded-replica metadata files (`*-wal`, `*-shm`, replica
   dirs) must not break bench backup/restore.

## Tasks (after analysis)

1. `frappe/database/sqlite/libsql_compat.py` — thin adapter giving libsql the
   exact sqlite3 surface our code uses (one place to absorb API drift).
2. Flip `database/sqlite/database.py` to the adapter behind common-config knob
   `sqlite_engine: "libsql" | "stdlib"` (default stdlib until phase end).
3. Flip `database/sqlite/aio.py`: aiosqlite → libsql thread-runner shim (keep
   PRAGMA set: cache_size -2048, mmap_size 0; re-tune for libsql, its
   defaults differ).
4. Flip `utils/sqlite_queue.py` (verify `UPDATE…RETURNING` + WAL +
   busy_timeout semantics identical; the startup reaper and Event-wake logic
   don't touch the driver).
5. Flip `search/sqlite_search.py` — FTS5 ships in libsql; run full search test suite.
6. Optional headline feature, separate commit: `libsql_replica_url` +
   `libsql_auth_token` in common config → site DB opened as embedded replica
   syncing to remote (the DR story). Light-mode-only; document.
7. Flip default `sqlite_engine: libsql`; stdlib path stays as rollback for one
   release; pyproject: add `libsql~=<picked>`, drop `aiosqlite` once aio.py no
   longer imports it.

## Risks

- Binding maturity: libsql python binding is younger than stdlib sqlite3 —
  the compat adapter + default-off knob contain the blast radius.
- Subtle behavior drift (type adapters/detect_types, text_factory, isolation
  level autocommit semantics) — sqlite3's implicit transaction handling is
  notoriously weird; the adapter must pin identical behavior, tested.
- Single-file artifacts (task_queue.db) shared by CLI (producer) and server
  (worker) — both must move engines in the same commit or wire-compat is
  required (it is: same file format, but lock/WAL interop between engines on
  the SAME live file must be verified explicitly).
- If wheels lag cp314t, this phase and Phase 23 conflict — resolve with build-from-source
  or sequencing (libsql after free-threading wheels exist).

## Verification

- Both-engine matrix on sqlite test suite: test_async_db_sqlite,
  test_sqlite_queue, test_sqlite_search, unit category on sqlite-async.local.
- Cross-engine file test: site DB created by stdlib → served by libsql → back.
- Concurrency soak: autocannon against sqlite-async.local + parallel enqueue
  (queue + DB on libsql simultaneously), zero `database is locked` regressions.
- Benchmark vs phase18 baseline on sqlite-async.local; RSS + rps in improvement.md.
- If replica enabled: kill local file, restore from replica, site boots.

## Rollback

`sqlite_engine: "stdlib"` in common config (until step 7's dep removal; after
that, revert the pyproject commit too).
