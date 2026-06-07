"""Phase 9: SQLite task queue (frappe.utils.sqlite_queue).

Workers are asyncio tasks, so these tests run on AsyncIntegrationTestCase's
loop and start/stop real workers against a temp queue DB.
"""

import asyncio
import sqlite3
import tempfile
import time
from unittest.mock import patch

import frappe
from frappe.config import patch_common_conf
from frappe.tests import AsyncIntegrationTestCase, IntegrationTestCase
from frappe.utils import sqlite_queue

PROBE_RESULTS = []
FAIL_COUNTS = {"n": 0}


def sync_probe(value=None):
	PROBE_RESULTS.append(("sync", value, frappe.local.site))


async def async_probe(value=None):
	count = await frappe.db.aio.count("User")
	PROBE_RESULTS.append(("async", value, count))


def failing_probe():
	FAIL_COUNTS["n"] += 1
	raise ValueError("probe exploded")


def _make_args(method, **kwargs):
	return {
		"site": frappe.local.site,
		"user": "Administrator",
		"method": method,
		"event": None,
		"job_name": "probe",
		"is_async": True,
		"kwargs": kwargs,
	}


class SQLiteQueueTestMixin:
	def use_tmp_queue_db(self):
		tmpdir = self.enterContext(tempfile.TemporaryDirectory())
		db_path = f"{tmpdir}/task_queue.db"
		self.enterContext(patch.object(sqlite_queue, "get_queue_db_path", lambda: db_path))
		return db_path

	def rows(self, db_path):
		conn = sqlite3.connect(db_path)
		conn.row_factory = sqlite3.Row
		try:
			return [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]
		finally:
			conn.close()


class TestSQLiteQueueProducer(SQLiteQueueTestMixin, IntegrationTestCase):
	def test_enqueue_inserts_row(self):
		db_path = self.use_tmp_queue_db()
		job = sqlite_queue.enqueue_job(_make_args(sync_probe, value=1), queue="default", job_id="s||j1")
		self.assertEqual(job.id, "s||j1")
		rows = self.rows(db_path)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["status"], "pending")
		self.assertEqual(rows[0]["queue"], "default")
		self.assertIn("sync_probe", rows[0]["func"])

	def test_duplicate_job_id_skipped_while_active(self):
		db_path = self.use_tmp_queue_db()
		self.assertIsNotNone(sqlite_queue.enqueue_job(_make_args(sync_probe), "default", "s||dup"))
		self.assertIsNone(sqlite_queue.enqueue_job(_make_args(sync_probe), "default", "s||dup"))
		self.assertEqual(len(self.rows(db_path)), 1)
		self.assertTrue(sqlite_queue.is_job_enqueued("s||dup"))
		self.assertFalse(sqlite_queue.is_job_enqueued("s||other"))

	def test_failed_row_cleared_on_reenqueue(self):
		db_path = self.use_tmp_queue_db()
		sqlite_queue.enqueue_job(_make_args(sync_probe), "default", "s||f1")
		conn = sqlite3.connect(db_path, isolation_level=None)
		conn.execute("UPDATE jobs SET status='failed'")
		conn.close()
		self.assertIsNotNone(sqlite_queue.enqueue_job(_make_args(sync_probe), "default", "s||f1"))
		rows = self.rows(db_path)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["status"], "pending")

	def test_frappe_enqueue_backend_switch(self):
		db_path = self.use_tmp_queue_db()
		with patch_common_conf(queue_backend="sqlite"):
			job = frappe.enqueue("frappe.tests.test_sqlite_queue.sync_probe", value=42)
		self.assertIsInstance(job, sqlite_queue.SQLiteJob)
		rows = self.rows(db_path)
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["job_id"].startswith(frappe.local.site))


class TestSQLiteQueueWorkers(SQLiteQueueTestMixin, AsyncIntegrationTestCase):
	async def asyncSetUp(self):
		await super().asyncSetUp()
		self.db_path = self.use_tmp_queue_db()
		PROBE_RESULTS.clear()
		FAIL_COUNTS["n"] = 0

	async def asyncTearDown(self):
		await sqlite_queue.stop_workers()
		await super().asyncTearDown()

	async def _wait(self, predicate, timeout=20):
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			if predicate():
				return
			await asyncio.sleep(0.05)
		self.fail("condition not reached within timeout")

	async def test_sync_job_executes(self):
		sqlite_queue.enqueue_job(_make_args(sync_probe, value="a"), "default", "s||w1")
		await sqlite_queue.start_workers(num_workers=2)
		await self._wait(lambda: PROBE_RESULTS)
		self.assertEqual(PROBE_RESULTS[0], ("sync", "a", frappe.local.site))
		self.assertEqual(self.rows(self.db_path), [])  # success rows deleted

	async def test_async_job_awaited(self):
		sqlite_queue.enqueue_job(_make_args(async_probe, value="b"), "default", "s||w2")
		await sqlite_queue.start_workers(num_workers=2)
		await self._wait(lambda: PROBE_RESULTS)
		kind, value, count = PROBE_RESULTS[0]
		self.assertEqual((kind, value), ("async", "b"))
		self.assertGreaterEqual(count, 1)
		self.assertEqual(self.rows(self.db_path), [])

	async def test_wake_event_picks_up_new_job(self):
		await sqlite_queue.start_workers(num_workers=1)
		sqlite_queue.enqueue_job(_make_args(sync_probe, value="late"), "default", "s||w3")
		await self._wait(lambda: PROBE_RESULTS)
		self.assertEqual(PROBE_RESULTS[0][1], "late")

	async def test_retries_then_failed_with_traceback(self):
		sqlite_queue.enqueue_job(_make_args(failing_probe), "default", "s||w4", max_retries=2)
		await sqlite_queue.start_workers(num_workers=1)
		await self._wait(lambda: self.rows(self.db_path) and self.rows(self.db_path)[0]["status"] == "failed")
		row = self.rows(self.db_path)[0]
		self.assertEqual(row["retries"], 2)
		self.assertEqual(FAIL_COUNTS["n"], 2)
		self.assertIn("probe exploded", row["error"])
		self.assertIsNotNone(row["ended_at"])

	async def test_reaper_requeues_stuck_running(self):
		sqlite_queue.enqueue_job(_make_args(sync_probe, value="stuck"), "default", "s||w5")
		conn = sqlite3.connect(self.db_path, isolation_level=None)
		conn.execute("UPDATE jobs SET status='running', started_at=datetime('now')")
		conn.close()
		await sqlite_queue.start_workers(num_workers=1)
		await self._wait(lambda: PROBE_RESULTS)
		self.assertEqual(PROBE_RESULTS[0][1], "stuck")

	async def test_job_runs_without_ambient_context(self):
		"""Regression: prod worker tasks run in the lifespan context, which has
		no frappe.local — method resolution (frappe.get_attr reads local.flags)
		must happen inside the job's own context, not the worker's. Caught live:
		every string-method job failed with AttributeError('flags') while these
		tests passed on the runner's ambient context."""
		from frappe.database.aio import run_in_clean_context

		sqlite_queue.enqueue_job(
			_make_args("frappe.tests.test_sqlite_queue.sync_probe", value="ctx"), "default", "s||ctx"
		)
		await sqlite_queue.start_workers(num_workers=0)  # conn only, no worker tasks
		row = await sqlite_queue._claim()
		await run_in_clean_context(sqlite_queue._run_job(row))
		self.assertEqual(PROBE_RESULTS[0][1], "ctx")
		self.assertEqual(self.rows(self.db_path), [])

	async def test_atomic_claim_no_double_grab(self):
		for i in range(8):
			sqlite_queue.enqueue_job(_make_args(sync_probe, value=i), "default", f"s||c{i}")
		await sqlite_queue.start_workers(num_workers=4)
		await self._wait(lambda: len(PROBE_RESULTS) >= 8 and not self.rows(self.db_path))
		values = sorted(r[1] for r in PROBE_RESULTS)
		self.assertEqual(values, list(range(8)))  # each job ran exactly once
