"""Phase 8: async def whitelisted methods.

Both forms side by side — sync handlers run in the worker thread pool
(zero changes for third-party apps), async handlers run on the event loop
and use the awaitable DB API:

	@frappe.whitelist()
	def sync_handler():
		return frappe.db.get_value("User", "Administrator", "name")

	@frappe.whitelist()
	async def async_handler():
		return await frappe.db.aio.get_value("User", "Administrator", "name")

Parallel calls inside an async handler use asyncio.gather:

	@frappe.whitelist()
	async def dashboard():
		users, doctypes = await asyncio.gather(
			frappe.db.aio.count("User"),
			frappe.db.aio.count("DocType"),
		)
		return {"users": users, "doctypes": doctypes}

Deliberately after the DB phases: the sync facade now raises from a loop
thread (fail-fast guard in Database.sql) instead of silently blocking the
whole process, and `frappe.db.aio` is a real awaitable API.
"""

import asyncio
import threading

import frappe
from frappe.dispatch import register_loop_thread
from frappe.handler import execute_cmd
from frappe.tests import IntegrationTestCase

HANDLER_PATH = "frappe.tests.test_async_whitelist"


@frappe.whitelist()
def sample_sync():
	return frappe.db.get_value("User", "Administrator", "name")


@frappe.whitelist()
async def sample_async():
	return await frappe.db.aio.get_value("User", "Administrator", "name")


@frappe.whitelist()
async def sample_gather():
	users, admin = await asyncio.gather(
		frappe.db.aio.count("User"),
		frappe.db.aio.get_value("User", "Administrator", "name"),
	)
	return {"users": users, "admin": admin}


class TestAsyncWhitelist(IntegrationTestCase):
	"""execute_cmd -> dispatch_sync is the production request path shape."""

	def setUp(self):
		from frappe.utils import set_request

		set_request(method="POST", path="/api/method/ping")
		frappe.set_user("Administrator")
		frappe.local.form_dict = frappe._dict()

	def test_sync_handler_unchanged(self):
		self.assertEqual(execute_cmd(f"{HANDLER_PATH}.sample_sync"), "Administrator")

	def test_async_handler_via_dispatch(self):
		self.assertEqual(execute_cmd(f"{HANDLER_PATH}.sample_async"), "Administrator")

	def test_async_handler_gather(self):
		result = execute_cmd(f"{HANDLER_PATH}.sample_gather")
		self.assertEqual(result["admin"], "Administrator")
		self.assertGreaterEqual(result["users"], 2)

	def test_sync_db_call_on_loop_thread_raises(self):
		"""The guard: sync frappe.db on a registered loop thread fails fast
		instead of blocking the process. Run in a throwaway thread so the
		test runner's own thread never lands in the registry."""
		result = {}
		db = frappe.local.db  # bind the real Database object for the bare thread

		def loop_thread():
			register_loop_thread()
			try:
				db.sql("select 1")
				result["error"] = None
			except RuntimeError as e:
				result["error"] = str(e)

		thread = threading.Thread(target=loop_thread)
		thread.start()
		thread.join()
		self.assertIn("frappe.db.aio", result["error"] or "")
