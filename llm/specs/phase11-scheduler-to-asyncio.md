# Phase 11: Scheduler to asyncio

**Part:** Queue & scheduler
**Depends on:** Phase 10
**Usable after:** yes.

Own phase — scheduler has its own semantics, not a queue footnote.

## Tasks

- Port scheduler tick loop to an asyncio task in the main loop (lifespan startup).
- Preserve: tick interval/lock semantics, per-site iteration + enqueue, missed-tick/catch-up behavior, `scheduler_disabled` flags.
- Old scheduler process remains runnable until cleanup phase — config flip reverts.
