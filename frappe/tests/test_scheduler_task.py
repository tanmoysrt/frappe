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

	async def test_config_flip_disables(self):
		enq = self._patch(enabled=False)
		scheduler.start_scheduler_task()
		await asyncio.sleep(0.2)
		self.assertTrue(scheduler._task_state["task"].done())
		self.assertEqual(enq.call_count, 0)
		# and the lock was never taken
		self.assertFalse(scheduler.is_schduler_process_running())

	async def test_external_scheduler_lock_wins(self):
		"""When `bench schedule` already holds the FileLock, the in-process
		task must stand down (never double-schedule)."""
		enq = self._patch()
		external = FileLock(scheduler._get_scheduler_lock_file(), thread_local=False)
		external.acquire(blocking=False)
		try:
			scheduler.start_scheduler_task()
			await asyncio.sleep(0.3)
			self.assertTrue(scheduler._task_state["task"].done())
			self.assertEqual(enq.call_count, 0)
		finally:
			external.release()

	async def test_lock_released_on_stop(self):
		enq = self._patch()
		scheduler.start_scheduler_task()
		for _ in range(100):
			await asyncio.sleep(0.05)
			if enq.call_count:
				break
		self.assertTrue(scheduler.is_schduler_process_running())  # we hold it
		await scheduler.stop_scheduler_task()
		self.assertFalse(scheduler.is_schduler_process_running())  # released
