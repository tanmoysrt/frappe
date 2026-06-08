"""Phase 11: in-process asyncio scheduler tick (frappe.utils.scheduler).

The tick math, per-site enqueue and disabled-flag logic are the existing
(unchanged) sync functions with their own tests; these cover the asyncio
wrapper: tick cadence, the cross-process FileLock and the config flip.
"""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

from filelock import FileLock

from frappe.tests import AsyncUnitTestCase
from frappe.utils import scheduler


class TestInProcessScheduler(AsyncUnitTestCase):
	async def asyncTearDown(self):
		await scheduler.stop_scheduler_task()
		await super().asyncTearDown()

	def _patch(self, enabled=True, tick_sleep=0.01):
		# private lock file: the real one may be held by a live server
		# (that's the production point of the lock, but it would couple
		# these tests to whatever else runs on the bench)
		tmpdir = self.enterContext(tempfile.TemporaryDirectory())
		lock_path = os.path.join(tmpdir, "scheduler_process")
		self.enterContext(patch.object(scheduler, "_get_scheduler_lock_file", lambda: lock_path))
		enq = MagicMock()
		self.enterContext(patch.object(scheduler, "in_process_scheduler_enabled", return_value=enabled))
		self.enterContext(patch.object(scheduler, "sleep_duration", return_value=tick_sleep))
		self.enterContext(patch.object(scheduler, "enqueue_events_for_all_sites", enq))
		return enq

	async def test_ticks_repeatedly(self):
		enq = self._patch()
		scheduler.start_scheduler_task()
		for _ in range(100):
			await asyncio.sleep(0.05)
			if enq.call_count >= 2:
				break
		self.assertGreaterEqual(enq.call_count, 2)

	async def test_config_disabled_skips_but_loop_stays_alive(self):
		"""`in_process_scheduler: 0` must NOT kill the task (old behaviour) — the
		loop keeps ticking and just skips the action, re-checking config each
		tick. The lock is never taken while disabled."""
		enq = self._patch(enabled=False)
		scheduler.start_scheduler_task()
		await asyncio.sleep(0.2)
		self.assertFalse(scheduler._task_state["task"].done())  # still ticking
		self.assertEqual(enq.call_count, 0)
		self.assertFalse(scheduler.is_schduler_process_running())  # lock untouched

	async def test_config_flip_resumes_without_restart(self):
		"""The whole point: flip the config flag at runtime and the always-on
		loop starts enqueuing at the next tick — no restart."""
		flag = MagicMock(return_value=False)
		tmpdir = self.enterContext(tempfile.TemporaryDirectory())
		self.enterContext(
			patch.object(scheduler, "_get_scheduler_lock_file", lambda: os.path.join(tmpdir, "lk"))
		)
		self.enterContext(patch.object(scheduler, "in_process_scheduler_enabled", flag))
		self.enterContext(patch.object(scheduler, "sleep_duration", return_value=0.01))
		enq = MagicMock()
		self.enterContext(patch.object(scheduler, "enqueue_events_for_all_sites", enq))

		scheduler.start_scheduler_task()
		await asyncio.sleep(0.1)
		self.assertEqual(enq.call_count, 0)  # disabled -> no action
		flag.return_value = True  # admin flips the config
		for _ in range(100):
			await asyncio.sleep(0.05)
			if enq.call_count:
				break
		self.assertGreaterEqual(enq.call_count, 1)  # resumed, no restart

	async def test_external_scheduler_lock_wins_then_hands_off(self):
		"""While `bench schedule` holds the FileLock the in-process task stands
		down (never double-schedule); when the external lock releases, the
		always-on loop takes over at the next tick."""
		enq = self._patch()
		external = FileLock(scheduler._get_scheduler_lock_file(), thread_local=False)
		external.acquire(blocking=False)
		scheduler.start_scheduler_task()
		await asyncio.sleep(0.2)
		self.assertFalse(scheduler._task_state["task"].done())  # alive, not exited
		self.assertEqual(enq.call_count, 0)  # stood down while external holds lock
		external.release()  # external scheduler stops
		for _ in range(100):
			await asyncio.sleep(0.05)
			if enq.call_count:
				break
		self.assertGreaterEqual(enq.call_count, 1)  # handed off without restart

	async def test_lock_released_after_stop(self):
		"""Per-tick lock must not leak: after stop, nothing holds it."""
		enq = self._patch()
		scheduler.start_scheduler_task()
		for _ in range(100):
			await asyncio.sleep(0.05)
			if enq.call_count:
				break
		await scheduler.stop_scheduler_task()
		self.assertFalse(scheduler.is_schduler_process_running())  # released
