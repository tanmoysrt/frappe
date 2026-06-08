# Phase 5: Async DB — SQLite (aiosqlite) Proving Ground

**Part:** Async core
**Depends on:** Phase 1
**Usable after:** yes — flag-gated per site.

SQLite is the cheap proving ground for the async `Database` class — simplest driver, no pooling, validates the async-first pattern + sync facade end-to-end before touching MariaDB.

## Tasks

- `Database` class async-first: primary `async def get_value(...)` etc.
- SQLite backend on `aiosqlite` — per-site connection, WAL mode. No pool.
- **Memory footprint**: each connection's page cache defaults to ~2 MB; one conn per site (DB) means it multiplies silently across many sites. **Explicitly set** `PRAGMA cache_size` (modest) and decide `mmap_size` deliberately rather than inheriting defaults.
- Sync compat: asgiref `async_to_sync` facade on every public method — existing sync code calls `frappe.db.get_value(...)` unchanged.
- Port transaction semantics: `commit()/rollback()/savepoint`, `after_commit` hooks.
