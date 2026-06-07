# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt
"""Phase 24.4: RQ Job desk doctype is backend-aware — when queue_backend is
'sqlite', get_list / load_from_db read the sqlite `jobs` table instead of redis.
No RQ worker required (unlike test_rq_job.py's redis path)."""

import frappe
from frappe.config import patch_common_conf
from frappe.core.doctype.rq_job.rq_job import RQJob, serialize_sqlite_job
from frappe.tests import IntegrationTestCase
from frappe.utils import sqlite_queue


class TestRQJobSQLite(IntegrationTestCase):
	def setUp(self):
		self.enterContext(patch_common_conf(queue_backend="sqlite"))
		self.job_id = f"{frappe.local.site}::test-sqlite-{frappe.generate_hash(length=8)}"
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		conn = sqlite_queue._connect()
		try:
			conn.execute("DELETE FROM jobs WHERE job_id = ?", (self.job_id,))
		finally:
			conn.close()

	def _enqueue(self, queue="default", job_name="sqlite-test-job"):
		queue_args = {
			"site": frappe.local.site,
			"user": "Administrator",
			"method": "frappe.ping",
			"job_name": job_name,
			"kwargs": {},
		}
		sqlite_queue.enqueue_job(queue_args, queue, self.job_id)

	def test_sqlite_job_appears_in_get_list(self):
		self._enqueue()
		jobs = RQJob.get_list(filters=None, start=0, page_length=50)
		match = [j for j in jobs if j.job_id == self.job_id]
		self.assertEqual(len(match), 1, "enqueued sqlite job not visible in RQ Job get_list")
		# pending -> queued status mapping, job_name from queue_args
		self.assertEqual(match[0].status, "queued")
		self.assertEqual(match[0].job_name, "sqlite-test-job")
		self.assertEqual(match[0].queue, "default")

	def test_sqlite_load_from_db(self):
		self._enqueue()
		doc = frappe.get_doc("RQ Job", self.job_id)
		self.assertEqual(doc.job_id, self.job_id)
		self.assertEqual(doc.status, "queued")

	def test_sqlite_load_missing_raises(self):
		with self.assertRaises(frappe.DoesNotExistError):
			frappe.get_doc("RQ Job", f"{frappe.local.site}::does-not-exist")

	def test_status_filter(self):
		self._enqueue()
		# queued matches, failed does not
		queued = RQJob.get_list(filters=[["RQ Job", "status", "=", "queued"]], page_length=50)
		self.assertTrue(any(j.job_id == self.job_id for j in queued))
		failed = RQJob.get_list(filters=[["RQ Job", "status", "=", "failed"]], page_length=50)
		self.assertFalse(any(j.job_id == self.job_id for j in failed))

	def test_serialize_failed_job_maps_status(self):
		# a failed row carries error text and maps to RQ's "failed"
		conn = sqlite_queue._connect()
		try:
			row = conn.execute(
				"SELECT 'x' AS job_id, ? AS site, 'long' AS queue, 'frappe.ping' AS func, "
				"X'80' AS kwargs, 'failed' AS status, 0 AS retries, 3 AS max_retries, "
				"'boom' AS error, datetime('now') AS enqueued_at, NULL AS started_at, "
				"NULL AS ended_at",
				(frappe.local.site,),
			).fetchone()
		finally:
			conn.close()
		job = serialize_sqlite_job(row)
		self.assertEqual(job.status, "failed")
		self.assertEqual(job.exc_info, "boom")
		self.assertEqual(job.queue, "long")
