# Phase 17: Async ORM — Lifecycle + doc_events

**Part:** ORM
**Depends on:** Phase 16
**Usable after:** yes — full async ORM, sync API still passes existing suite.

## Tasks

- `submit` / `cancel` / workflow transitions async-primary.
- `doc_events` hooks dispatched via unwrap detection — sync hooks unchanged.
- Verify no deadlocks mixing sync/async ORM calls in one request.
