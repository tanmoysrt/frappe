# Phase 4: aiofiles in Hot Paths

**Part:** Async core
**Depends on:** Phase 1
**Usable after:** yes.

## Tasks

- `aiofiles` for file uploads, site config reads, file writes in request path.
- Sync call-sites untouched (they run in thread pool anyway from Phase 1).
