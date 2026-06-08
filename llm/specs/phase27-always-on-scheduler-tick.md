# Phase 27: Always-on in-process scheduler tick (config decides per tick)

**Part:** Scheduler resilience (follow-up to Phase 11)
**Depends on:** Phase 11 (in-process asyncio scheduler) + Phase 26.2 (lifespan
starts the tick task).
**Usable after:** yes — same `bench serve`, same config keys.

## Problem

Phase 11's `_scheduler_loop` made its run/skip decision **once, at startup**, in
a `_setup()` step:

- `in_process_scheduler: 0` in common config → `_setup` returned `None` → the
  task **exited immediately and never ran again**.
- the cross-process `FileLock` was acquired once and **held for the task's whole
  life**; if an external `bench schedule` already held it at boot, the
  in-process task gave up **permanently**.

Consequence: changing `in_process_scheduler`, or starting/stopping an external
`bench schedule`, required a **server restart** to take effect. The loop was not
self-correcting.

## Goal

The tick loop should **always run** while the server is up; on **each tick** it
decides from current state whether to take action:

1. read `in_process_scheduler` — if disabled this tick, skip (don't exit).
2. try the cross-process `FileLock` non-blocking — if an external scheduler
   holds it this tick, stand down; else acquire, enqueue, release.

So a runtime config flip or an external scheduler stopping is honoured at the
**next tick**, no restart.

## Invariant preserved

Enqueue runs **only while holding the lock**, now acquired/released per tick
(non-blocking). The external `bench schedule` holds the same lock for its whole
life, and acquisition is non-blocking on both sides, so at most one holder
exists at any instant → the in-process and external schedulers can **never
enqueue at the same time**. The only new behaviour is a graceful hand-off: if
the external scheduler stops, the in-process loop picks up at the next tick
(previously it stayed dead).

## Changes

### `frappe/utils/scheduler.py`
- `_scheduler_loop`: drop the one-shot `_setup` gate. The loop always runs; a
  per-tick `_tick()` (on a pool thread, fresh contextvars Context) re-reads the
  config flag, takes the FileLock non-blocking, enqueues only if acquired, and
  releases in `finally`. `_tick` returns the (possibly changed) tick interval so
  the cadence also tracks `scheduler_tick_interval` changes.
- `_task_state` loses the `"lock"` entry — no lock is held between ticks, so
  there is nothing for `stop_scheduler_task` to release (the per-tick `finally`
  already releases it; a cancel during a running tick lets the pool thread
  finish and release).

Per-site checks (`maintenance_mode`, `pause_scheduler`,
`SystemSettings.enable_scheduler`) were already re-evaluated per tick inside
`enqueue_events_for_site` / `is_scheduler_inactive` — unchanged.

## Verification

`frappe/tests/test_scheduler_task.py` (rewritten to the always-on contract):
- `test_ticks_repeatedly` — still enqueues each tick (unchanged).
- `test_config_disabled_skips_but_loop_stays_alive` — `in_process_scheduler: 0`:
  task is **not** done, 0 enqueues, lock untouched.
- `test_config_flip_resumes_without_restart` — flip the flag at runtime → loop
  starts enqueuing at the next tick (the core win).
- `test_external_scheduler_lock_wins_then_hands_off` — external lock held: task
  alive + 0 enqueues; release it → in-process takes over without restart.
- `test_lock_released_after_stop` — per-tick lock does not leak after stop.

5/5 green on minimal.local.

## Non-goals

- No change to tick math, per-site enqueue, or the disabled-flag semantics.
- No new config keys.
