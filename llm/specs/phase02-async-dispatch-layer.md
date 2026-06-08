# Phase 2: Async Dispatch Layer

**Part:** Foundation
**Depends on:** Phase 1
**Usable after:** yes — all handlers still sync, just routed through dispatch.

Routing knows about async, but no handler is async yet.

## Tasks

- `is_async_callable()` via `inspect.unwrap` — handles `@wraps`, `functools.partial`, `__call__`, `@frappe.whitelist()` chains.
- `dispatch()`: async handler → `await`; sync handler → `sync_to_async(...)` into the pool.
- Wire `handle_rpc` / `handle_rest` through `dispatch()`.

## Compat mechanism (asgiref, both directions)

- **Sync handler → async core**: sync code always runs in the thread pool (never on the loop thread). From there, the sync facade of each API bridges back to the main loop with `asgiref.sync.async_to_sync(async_impl)(...)` — safe because the loop runs in a different thread.
- **Async handler → async core**: plain `await`, zero overhead, no bridge.
- **Detection**: `is_async_callable()` via `inspect.unwrap` decides the path once at dispatch; APIs expose one async implementation + thin `async_to_sync` facade.
- **Fail-fast guard**: calling a sync facade from the loop thread raises — asgiref's `async_to_sync` errors when a loop is already running in the current thread. Converts "async handler silently blocks loop on sync call" footgun into immediate exception.
- **Deprecation pressure, not breakage**: sync facade logs a "port to async" hint; never removed without a major version.
