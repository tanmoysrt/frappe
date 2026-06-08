# Phase 3: Async Redis Cache

**Part:** Async core
**Depends on:** Phase 1
**Usable after:** yes — sync cache calls keep working through wrapper.

## Tasks

- Cache layer on `redis.asyncio`.
- Async-primary API: `await frappe.cache().get_value(...)`.
- Sync compat via asgiref `async_to_sync` facade.
- No-loop case (bench CLI, patches call cache too): `async_to_sync` spins up a loop — verify, same as the DB phases.
