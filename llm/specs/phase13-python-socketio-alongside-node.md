# Phase 13: Python Socket.IO Alongside Node.js

**Part:** Realtime
**Depends on:** Phase 1
**Usable after:** yes — Node.js still serves clients.

Runs in same uvicorn loop; Node.js stays up during transition.

## Tasks

- `frappe/realtime_server.py`: `socketio.AsyncServer(async_mode="asgi")`.
- Connect handler: site namespace validation, origin check, cookie/auth parse, direct `frappe.realtime.get_user_info()` call — no HTTP round-trip.
- Port handlers: `ping`, `doctype_subscribe`, `doc_subscribe`, `doc_open`, `doc_close`, `task_subscribe`, `disconnect`, doc-viewer notify.
- Mount in ASGI app: `/socket.io` → `socketio.ASGIApp(sio)`, else HTTP app. One process, two tasks, one loop.
- Keep Node.js running on its port; nothing switches yet.
