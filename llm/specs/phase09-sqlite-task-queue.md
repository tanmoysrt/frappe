# Phase 9: SQLite Task Queue Alongside RQ

**Part:** Queue & scheduler
**Depends on:** Phase 1
**Usable after:** yes — RQ remains default; opt in per site.

New queue ships next to RQ — no removal yet.

## Tasks

- `jobs` table: queue/func/kwargs/status/retries/error/timestamps; indexes `(status)`, `(queue, status)`.
- WAL mode, `busy_timeout=5000`, 3–4 in-process asyncio workers, `asyncio.Event` wake signal.
- **Memory**: set `PRAGMA cache_size` / `mmap_size` explicitly on the queue connection (same reasoning as Phase 5 — don't inherit the ~2 MB default blind).
- Retry `max_retries=3`; failed jobs keep traceback.
- **Stuck-job reaper** — single process means no separate worker outlives a crash. If the process dies mid-job, rows stay `status='running'` forever (and the RSS soft-restart in Phase 1 *will* kill in-flight jobs). On startup, requeue: `status='running'` older than a timeout → back to `pending`. Without this, the memory-hygiene soft-restart silently drops jobs.
- **Atomic job claim** — research's fetch-then-update has an `await` between SELECT and UPDATE, so two in-loop workers can grab the same job; use single `UPDATE ... SET status='running' WHERE id = (SELECT id ... LIMIT 1) RETURNING ...` (requires SQLite ≥ 3.35).
- Workers are asyncio tasks in the main loop (started at lifespan startup) — same process as HTTP, shared memory, no worker process.
- Executes async funcs (awaited) and sync funcs (`sync_to_async` into the shared pool — never `thread_sensitive=True`, or jobs serialize with web requests).
- `frappe.enqueue()` gains backend switch (site config): `rq` (default) | `sqlite`. API identical.
