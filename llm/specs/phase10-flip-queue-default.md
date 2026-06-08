# Phase 10: Flip Queue Default to SQLite (RQ stays, permanently)

**Part:** Queue & scheduler
**Depends on:** Phase 9
**Usable after:** yes — `frappe.enqueue()` unchanged for all callers, one flag picks the backend.

## Tasks

- Flip **default** backend to SQLite queue — the simplest thing for a fresh light-mode install: `python3 app.py`, no worker processes, no queue Redis.
- **RQ is a permanent, configurable backend** — large deployments set `queue_backend: rq` in site config and run `bench worker` processes as today. Not deprecated, not removed, ever — light vs heavy is a config choice.
- ARQ as a third opt-in backend for asyncio-native scale-out (multi-server, 10k+/sec, cron, arq-dashboard) — same `frappe.enqueue()`.
