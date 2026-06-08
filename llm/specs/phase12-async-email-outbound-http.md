# Phase 12: Async Email + Outbound HTTP

**Part:** Async core
**Depends on:** Phase 1
**Usable after:** yes.

Only depends on Phase 1 — can run parallel to queue work.

## Tasks

- `frappe.sendmail()` async-primary via aiosmtplib.
- Sync compat wrapper — existing call-sites unchanged.
- Same pattern for outbound HTTP: `httpx.AsyncClient` for async-primary call-sites (webhooks, integrations, OAuth); existing `requests` code untouched — runs in thread pool.
