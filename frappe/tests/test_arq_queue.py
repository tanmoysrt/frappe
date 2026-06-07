"""Phase 10: ARQ opt-in backend (frappe.utils.arq_queue) + the default flip.

The arq worker normally runs out-of-process (`arq
frappe.utils.arq_queue.WorkerSettings`); here a burst-mode Worker runs on
the test loop and drains the queue, which exercises the exact same task
function.
"""

import uuid
from unittest.mock import patch

from asgiref.sync import sync_to_async

import frappe
from frappe.tests import AsyncIntegrationTestCase, IntegrationTestCase
from frappe.utils import arq_queue
from frappe.utils.background_jobs import get_queue_backend

ARQ_PROBE = []


def arq_probe(value=None):
	ARQ_PROBE.append((value, frappe.local.site))


class TestQueueBackendDefault(IntegrationTestCase):
	def test_default_is_sqlite(self):
		frappe.conf.pop("queue_backend", None)
		self.assertEqual(get_queue_backend(), "sqlite")

	def test_rq_stays_configurable(self):
		frappe.conf["queue_backend"] = "rq"
		self.addCleanup(frappe.conf.pop, "queue_backend", None)
		self.assertEqual(get_queue_backend(), "rq")

	def test_unknown_backend_throws(self):
		frappe.conf["queue_backend"] = "carrier-pigeon"
		self.addCleanup(frappe.conf.pop, "queue_backend", None)
		self.assertRaises(frappe.ValidationError, frappe.enqueue, "frappe.handler.ping")


class TestArqQueue(AsyncIntegrationTestCase):
	async def asyncSetUp(self):
		await super().asyncSetUp()
		ARQ_PROBE.clear()
		# isolated arq stream per test — the pool is shared/bridge-bound,
		# but the queue name is read per call
		self.queue_name = f"frappe:test:{uuid.uuid4().hex[:8]}"
		self.enterContext(patch.object(arq_queue, "ARQ_QUEUE_NAME", self.queue_name))

	async def _drain(self):
		from arq.worker import Worker, func

		worker = Worker(
			functions=[func(arq_queue.run_frappe_job, name="run_frappe_job")],
			redis_settings=arq_queue._redis_settings(),
			queue_name=self.queue_name,
			burst=True,
			poll_delay=0.1,
		)
		try:
			await worker.main()
		finally:
			await worker.close()

	def _args(self, **kwargs):
		return {
			"site": frappe.local.site,
			"user": "Administrator",
			"method": "frappe.tests.test_arq_queue.arq_probe",
			"event": None,
			"job_name": "arq probe",
			"is_async": True,
			"kwargs": kwargs,
		}

	async def test_round_trip(self):
		enqueue = sync_to_async(arq_queue.enqueue_job, thread_sensitive=False)
		job = await enqueue(self._args(value="x"), queue="default", job_id=f"s||{uuid.uuid4().hex}")
		self.assertIsNotNone(job)
		await self._drain()
		self.assertEqual(ARQ_PROBE, [("x", frappe.local.site)])

	async def test_job_id_dedup(self):
		enqueue = sync_to_async(arq_queue.enqueue_job, thread_sensitive=False)
		job_id = f"s||dedup-{uuid.uuid4().hex[:8]}"
		first = await enqueue(self._args(), queue="default", job_id=job_id)
		second = await enqueue(self._args(), queue="default", job_id=job_id)
		self.assertIsNotNone(first)
		self.assertIsNone(second)  # arq's built-in job-id dedup
		is_enqueued = sync_to_async(arq_queue.is_job_enqueued, thread_sensitive=False)
		self.assertTrue(await is_enqueued(job_id))
		await self._drain()
		self.assertFalse(await is_enqueued(job_id))
