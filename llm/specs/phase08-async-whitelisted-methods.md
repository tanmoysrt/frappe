# Phase 8: Allow `async def` Whitelisted Methods

**Part:** Async core
**Depends on:** Phase 2, Phase 7
**Usable after:** yes — opt-in async endpoints; everything else as before.

First user-visible async feature. **Deliberately after the DB phases** — an async handler shipped before awaitable APIs exist would call sync `frappe.db` directly on the loop thread and silently block the whole process. Now the sync facade raises from the loop thread instead (fail-fast guard), and there are real `await frappe.db.*` / `await frappe.cache()` APIs to call.

## Tasks

- `@frappe.whitelist()` accepts `async def` (detection from Phase 2 already works) — both forms side by side:

```python
@frappe.whitelist()
def test():          # sync — dispatched via sync_to_async(test) into the thread pool
    return "ok"

@frappe.whitelist()
async def test2():   # async — awaited directly on the loop (native path)
    return "ok"
```

- Document `asyncio.gather()` pattern for parallel calls inside async handlers.
- Dev mode: `loop.slow_callback_duration` warning enabled — any remaining loop-blocking call shows up in logs.
- **Blocking-call CI/lint gate (cross-cutting)**: one `time.sleep` / `requests.*` / sync `open()` on an async path stalls the *entire* process — not just one worker, as today. `slow_callback_duration` is reactive (runtime, dev-only); add a static lint/CI check that flags known-blocking calls inside `async def` bodies so they never ship.
- Sync handlers untouched — `sync_to_async` path, third-party apps need zero changes.
