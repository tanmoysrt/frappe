# Phase 14: Switch Realtime Clients to Python (Node.js stays runnable)

**Part:** Realtime
**Depends on:** Phase 13
**Usable after:** yes.

## Tasks

- `publish_realtime()` → direct `loop.create_task(sio.emit(...))`; `after_commit` buffering via `frappe.db.after_commit`.
- Direct emit is safe by design — single process, single loop, all clients connected to this process. No Redis pub/sub.
- (Only if someone opts into multi-process scale-out later: sticky sessions + socketio `AsyncRedisManager`. Not the default, not this phase.)
- Point clients at Python server. **Node.js stays installed and runnable** — config flip reverts (rollback rule). Removal in cleanup phase.
