# Phase 7: Async DB — Out-of-Loop Verification (CLI/bench/patches)

**Part:** Async core
**Depends on:** Phase 6
**Usable after:** yes — pure verification + fixes.

CLI/bench/patches/migrate run outside the event loop — `async_to_sync` handles the no-loop case (spins one up). Dedicated phase because the surface is wide and failures are subtle.

## Tasks

- Verify `bench migrate`, `bench console`, patch runner, `bench execute`, scheduler-invoked code paths against both aiosqlite and aiomysql backends.
- Verify pool lifecycle outside lifespan (CLI creates + closes its own loop — pools must not leak).
