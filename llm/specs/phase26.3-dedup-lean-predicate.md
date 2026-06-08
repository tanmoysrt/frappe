# Phase 26.3: De-dup the lean-backend predicate in serve.py

**Part:** Cleanup (follow-up to Phase 26.2)
**Depends on:** Phase 26.2 complete (`frappe/serve.py` is the one server module).
**Usable after:** yes — pure refactor, no user-facing change.

## Goal

Remove a duplicated predicate in `frappe/serve.py`. The "lean backend set" test
(sqlite main DB + in-process/memory cache + sqlite queue) was written **twice**:

1. `_lean_backends(conf)` — module-level helper, used by `_reexec()` to default
   `use_tcmalloc` to `0` (glibc allocator, Phase 25.2) on lean sites.
2. An inline copy inside `serve()` (`lean = (...)`) used to cap the thread pool
   at 4 (Phase 24.10).

Two copies of the same 3-condition rule = drift hazard: change the allocator
rule and the pool-sizing rule silently diverges, even though they describe the
*same* notion of "this is the smallest, no-external-I/O deployment."

## Why it is safe to share

Both call sites pass a config object with a `.get` API and the same keys:
- `_reexec` → `_read_common_config()` (raw `json.load` dict).
- `serve()` → `frappe.get_common_site_config(sites_path)` (a `dict` subclass).

The predicate only reads `db_type`, `cache_backend`, `queue_backend`, so the two
object types are interchangeable for it.

## Changes

### `frappe/serve.py`
- Delete the inline `lean = (...)` block in `serve()`; call the existing
  `_lean_backends(conf)` helper instead.
- Comment notes the two uses share one predicate so they cannot drift.

No change to `_lean_backends` itself, to `_reexec`, or to any default.

## Verification

- `serve.py` parses clean; the inline `lean = (` block is gone; `serve()` calls
  `_lean_backends(conf)`.
- **Regression test:** `_lean_backends` returns the expected result across the
  backend matrix — sqlite-only, explicit `memory`+`sqlite`, empty-string
  backends, mariadb, redis cache, rq queue, empty conf. Lock these so the
  predicate (now load-bearing for both allocator choice and pool sizing) cannot
  silently change. Lives in `frappe/tests/test_serve_lean.py` and imports the
  helper without booting the uvicorn runtime.
- Gate: `test_import_budget` 4/4 (serve.py change must not pull new imports onto
  the `frappe.app` serving path), plus the new `test_serve_lean`.

## Non-goals

- No memory/perf delta — identical predicate, same defaults.
- Lazy meta loading and the other small-site memory levers (module-code `-OO`,
  babel localedata) are **deferred** (recorded in improvement.md, not this phase).
