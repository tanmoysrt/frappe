# Phase 28: async DB driver on by default

**Part:** Async-first (follow-up to Phases 5–8, 15–17)
**Depends on:** the async DB stack (bridge loop, fork quarantine, asgiref
bridging) — all shipped.
**Usable after:** yes — `use_async_db: 0` in common_site_config.json is the
explicit sync rollback.

## Goal

Make the async DB driver the **default**. Today `use_async_db` defaults falsy, so
a bench gets the sync drivers unless it opts in. The whole migration is
async-first and the async path is the asgiref-bridged one (sync callers reach it
via `run_coroutine_sync`, async callers via `sync_to_async` — the Django model);
the sync connection path stays only as an explicit rollback.

So: flip the default to on. `use_async_db: 0` becomes the opt-OUT.

## Why this is safe to default

- Non-server contexts (CLI, `bench worker` forks, patches) already work on
  async: `get_bridge_loop()` lazily starts a per-pid bridge-loop thread, and the
  fork quarantine + after-fork hooks + atexit pool close in `mariadb/aio.py`
  already make fork-during-CLI safe (was reproduced + fixed).
- New sites default to sqlite (Phase 24.10); sqlite async uses aiosqlite (worker
  thread + queue, no loop selector) — none of the aiomysql fork/gc hazards.
- The off-thread-gc wedge is handled (Phase 26 `gc_collect_safe`).
- This bench already runs `use_async_db: 1` explicitly, so the running config is
  unchanged; only benches that never set the flag change behaviour.

## Changes

### `frappe/database/__init__.py`
Add one helper, the single source of the default (mirrors
`scheduler.in_process_scheduler_enabled()`):

```python
def async_db_enabled() -> bool:
    """Async DB driver is the default (Phase 28). Set use_async_db: 0 in
    common_site_config.json for the sync rollback."""
    import frappe
    from frappe.utils import cint
    return cint(frappe.get_common_conf("use_async_db", 1)) == 1
```

`get_db` uses `async_db_enabled()` in all three backend branches instead of the
bare `frappe.get_common_conf("use_async_db")` truthy read.

### `frappe/utils/preload.py`
`use_async = async_db_enabled()` (so the lifespan warms the async driver by
default) instead of `conf("use_async_db")`.

### `frappe/serve.py`
The re-exec bootstrap is stdlib-only (runs before `import frappe`), so it can't
call the helper; just default the flag in place: `_flag(conf, "use_async_db", 1)`
in `_gil_env_decision` — keeps "no-GIL only with the async stack" consistent now
that async is assumed.

## Verification

`frappe/tests/test_async_db_default.py`:
- `async_db_enabled()` returns True when the flag is unset (default), True for
  `use_async_db: 1`, False for `use_async_db: 0`.
- gate: `test_import_budget` 4/4 (no new eager imports), and the existing async
  DB suites still green on the explicit-on bench.

## Non-goals

- Sync drivers stay (base classes + rollback) — not removed.
- `use_mysqlclient` / the MySQLdb driver untouched.
