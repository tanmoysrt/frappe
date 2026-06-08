# Phase 6: Async DB — MariaDB (aiomysql + per-site pools)

**Part:** Async core
**Depends on:** Phase 5
**Usable after:** yes — flag-gated per site, pymysql fallback one flip away.

MariaDB/MySQL is where Frappe usage actually is. Async-first `Database` shape already proven in Phase 5 — this phase is the driver + pooling.

## Tasks

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

## Thread-pool ↔ pool-maxsize coupling (correctness + memory)

- Every sync handler that calls a sync DB facade does `async_to_sync` back to the loop → **acquires an aiomysql pool conn**. If the thread pool (Phase 1, ~2×CPU) is larger than aggregate pool capacity for hot sites, threads block on pool acquire → 503s under sync-heavy load. **Rule: thread-pool concurrency must not exceed `Σ maxsize` over hot sites.**
- **Conservative `maxsize` default (~5)** — memory: aggregate live conns = `Σ maxsize over hot sites`; each conn holds buffers. Don't oversize.
- Confirm **idle-evict actually closes the pool** (frees its buffers), not just idles connections — cold sites must drop to zero resident pool memory.
