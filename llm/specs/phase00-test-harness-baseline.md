# Phase 0: Test Harness + Baseline

**Part:** Foundation
**Depends on:** —
**Usable after:** yes — nothing shipped, everything measured.

De-risks every later phase. No production code changes.

## Tasks

- **Async test support**: pick and wire the runner (pytest-asyncio or equivalent inside frappe's test runner) so `async def` tests run alongside sync tests. "All tests green every phase" needs this first.
- **Benchmark harness**: `autocannon -c10` against homepage, scripted + recorded per phase. References from this bench: gevent+ASGI ~119 rps / p50 82ms (4 workers); WSGI-serial 16 rps. Regressions become numbers, not vibes.
- **RSS capture (memory as a first-class metric)**: record resident memory alongside rps/p50, every phase. Without this "we reduced memory" is unfalsifiable.
  - **Baseline** = sum of RSS across today's whole stack: gunicorn web workers (2·CPU+1) + RQ workers (short/default/long) + Node.js socket.io. `ps -o rss= -p <pids>` summed.
  - **Per phase** = single-process RSS at idle AND under `-c10` load.
  - The headline win is process consolidation (~10–15 procs each carrying a full frappe+apps import → 1). Quantify it; track every later memory lever (thread-pool size, pool maxsize, malloc hygiene) against this number.
- **Spike test**: `frappe.local` (ContextVar) propagation across `sync_to_async(thread_sensitive=False)` → `async_to_sync` round-trips — set in sync code, read on loop, and back. The whole compat strategy rests on this; prove it before Phase 1.

## Ground rules (every phase)

1. Ship behind the existing API — no breaking change for apps.
2. All tests green before the phase ships.
3. Previous behavior one config flip away (rollback). Sole exception: Phase 18.
4. The framework is fully usable after every phase.
