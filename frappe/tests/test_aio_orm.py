"""Phase 15: awaitable ORM read facade — ``await frappe.aio.get_doc(...)``.

Sync ORM stays primary and unchanged; these prove the awaitable view works
from a running event loop (where the sync API would trip the Phase 8 loop
guard), shares the caller's request context, and serializes safely under
asyncio.gather (one connection, serial wire protocol).
"""

import asyncio

import frappe
from frappe.tests import AsyncIntegrationTestCase, IntegrationTestCase


@frappe.whitelist()
async def sample_orm_read():
	doc, count = await asyncio.gather(
		frappe.aio.get_doc("User", "Administrator"),
		frappe.db.aio.count("User"),
	)
	return {"name": doc.name, "users": count}


class TestAioWhitelisted(IntegrationTestCase):
	"""execute_cmd -> dispatch_sync: the production request path shape."""

	def test_async_handler_uses_orm_facade(self):
		from frappe.handler import execute_cmd
		from frappe.utils import set_request

		set_request(method="POST", path="/api/method/ping")
		frappe.set_user("Administrator")
		frappe.local.form_dict = frappe._dict()
		result = execute_cmd("frappe.tests.test_aio_orm.sample_orm_read")
		self.assertEqual(result["name"], "Administrator")
		self.assertGreaterEqual(result["users"], 2)


class TestAioReadPath(AsyncIntegrationTestCase):
	async def test_get_doc(self):
		doc = await frappe.aio.get_doc("User", "Administrator")
		self.assertEqual(doc.name, "Administrator")
		self.assertEqual(doc.doctype, "User")

	async def test_get_cached_doc(self):
		doc = await frappe.aio.get_cached_doc("User", "Administrator")
		self.assertEqual(doc.name, "Administrator")

	async def test_get_all_and_list(self):
		users = await frappe.aio.get_all("User", filters={"name": "Administrator"}, limit=1)
		self.assertEqual(users[0].name, "Administrator")
		listed = await frappe.aio.get_list("User", filters={"name": "Administrator"})
		self.assertEqual(listed[0].name, "Administrator")

	async def test_get_value(self):
		self.assertEqual(await frappe.aio.get_value("User", "Administrator", "name"), "Administrator")
		self.assertEqual(
			await frappe.aio.get_cached_value("User", "Administrator", "user_type"), "System User"
		)

	async def test_get_single_value(self):
		# any Single works; System Settings always exists. The sync comparison
		# value comes via db.aio — plain frappe.db on the loop trips the guard.
		country = await frappe.aio.get_single_value("System Settings", "country")
		self.assertEqual(country, await frappe.db.aio.get_single_value("System Settings", "country"))

	async def test_get_meta(self):
		meta = await frappe.aio.get_meta("User")
		self.assertTrue(meta.has_field("email"))

	async def test_gather_is_serialized_and_correct(self):
		# one request = one connection; the facade must serialize, not corrupt
		names = ["Administrator", "Guest"] * 3
		docs = await asyncio.gather(*(frappe.aio.get_doc("User", n) for n in names))
		self.assertEqual([d.name for d in docs], names)

	async def test_shares_request_context(self):
		# the facade sees the caller's frappe.local — not a clean context
		frappe.local.aio_probe = "x"

		def read_probe():
			return frappe.local.aio_probe

		from asgiref.sync import sync_to_async

		self.assertEqual(await sync_to_async(read_probe, thread_sensitive=False)(), "x")
		doc = await frappe.aio.get_doc("User", frappe.session.user)
		self.assertEqual(doc.name, frappe.session.user)

	async def test_loop_guard_still_active(self):
		# regression: direct sync DB on the loop must still fail fast
		from frappe import dispatch

		if not dispatch._loop_thread_ids:
			import threading

			dispatch._loop_thread_ids.add(threading.get_ident())
			self.addCleanup(dispatch._loop_thread_ids.discard, threading.get_ident())
		with self.assertRaises(RuntimeError):
			frappe.db.sql("select 1")
		# while the facade works fine from the same loop
		self.assertTrue(await frappe.aio.get_value("User", "Administrator", "name"))
