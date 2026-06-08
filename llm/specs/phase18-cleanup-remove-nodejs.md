# Phase 18: Cleanup — Remove Node.js realtime

**Part:** Cleanup
**Depends on:** Phase 14
**Usable after:** yes — nothing left depends on what's removed.

The only phase allowed to break rollback — runs last, after Python socket.io has proven stable in production.

## Tasks

- Remove Node.js `socketio.js` + `realtime/` — Python server covers both light mode (direct emit) and scale-out (opt-in `AsyncRedisManager`).
- **RQ and `bench worker` are NOT removed** — permanent backend for large deployments (Phase 10).
- Light mode after this: `python3 app.py` runs everything in one process.
