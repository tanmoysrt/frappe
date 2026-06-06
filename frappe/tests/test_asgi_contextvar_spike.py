"""Phase 0 spike: prove `frappe.local` (ContextVar-backed) propagates across
asgiref's sync/async bridges the way the WSGI->ASGI compat strategy requires.

The whole compat model rests on this: `Local` (frappe/utils/local.py) stores a
single mutable dict in one ContextVar. asgiref copies the contextvars Context
into the `sync_to_async(thread_sensitive=False)` worker thread — the copy holds
a reference to the SAME dict, so reads and writes are shared end to end.
"""

from asgiref.sync import async_to_sync, sync_to_async

import frappe
from frappe.tests import AsyncUnitTestCase
from frappe.utils.local import _contextvar


class TestAsgiContextVarSpike(AsyncUnitTestCase):
	def tearDown(self):
		for key in ("spike_key", "rt_key", "set_on_loop"):
			try:
				delattr(frappe.local, key)
			except AttributeError:
				pass
		super().tearDown()

	async def test_async_tests_are_awaited(self):
		"""Canary: plain unittest.TestCase silently passes un-awaited async
		tests. This assertion only runs if the runner truly awaits."""
		awaited = []

		async def mark():
			awaited.append(True)

		await mark()
		self.assertEqual(awaited, [True])

	async def test_set_on_loop_visible_in_thread(self):
		@sync_to_async(thread_sensitive=False)
		def read_in_thread():
			return getattr(frappe.local, "spike_key", "<missing>")

		frappe.local.spike_key = "set-on-loop"
		self.assertEqual(await read_in_thread(), "set-on-loop")

	async def test_thread_mutation_visible_on_loop(self):
		@sync_to_async(thread_sensitive=False)
		def mutate_in_thread():
			frappe.local.spike_key = "mutated-in-thread"

		frappe.local.spike_key = "initial"
		await mutate_in_thread()
		self.assertEqual(frappe.local.spike_key, "mutated-in-thread")

	def test_full_round_trip_sync_loop_thread_sync(self):
		"""sync -> async_to_sync -> loop -> sync_to_async thread -> back to
		sync. Asserts the SAME mutable dict is shared on every hop."""

		@sync_to_async(thread_sensitive=False)
		def thread_read_and_mutate():
			seen = getattr(frappe.local, "rt_key", "<missing>")
			frappe.local.rt_key = "from-thread"
			return seen, id(_contextvar.get(None))

		async def on_loop():
			loop_seen = getattr(frappe.local, "rt_key", "<missing>")
			frappe.local.set_on_loop = True
			thread_seen, thread_dict_id = await thread_read_and_mutate()
			return loop_seen, thread_seen, thread_dict_id

		frappe.local.rt_key = "from-sync"
		sync_dict_id = id(_contextvar.get(None))

		loop_seen, thread_seen, thread_dict_id = async_to_sync(on_loop)()

		# propagation forward: sync -> loop -> thread
		self.assertEqual(loop_seen, "from-sync")
		self.assertEqual(thread_seen, "from-sync")
		# mutations propagate back to sync land
		self.assertEqual(frappe.local.rt_key, "from-thread")
		self.assertTrue(getattr(frappe.local, "set_on_loop", False))
		# same mutable dict object end to end (the crux of the compat model)
		self.assertEqual(sync_dict_id, thread_dict_id)
