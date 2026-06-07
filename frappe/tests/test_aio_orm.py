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


class TestAioWritePath(AsyncIntegrationTestCase):
	"""Phase 16: await doc.aio.insert()/save()/delete() + async controller
	hooks dispatched from the sync ORM core."""

	async def test_insert_save_delete(self):
		doc = await frappe.aio.new_doc("ToDo")
		doc.description = "phase16 aio write"
		await doc.aio.insert()
		self.assertTrue(doc.name)
		name = doc.name
		self.assertEqual(await frappe.aio.get_value("ToDo", name, "description"), "phase16 aio write")

		doc.description = "phase16 updated"
		await doc.aio.save()
		self.assertEqual(await frappe.aio.get_value("ToDo", name, "description"), "phase16 updated")

		await doc.aio.delete()
		self.assertIsNone(await frappe.aio.get_value("ToDo", name, "description"))

	async def test_gather_writes_serialized(self):
		docs = []
		for i in range(4):
			d = await frappe.aio.new_doc("ToDo")
			d.description = f"phase16 gather {i}"
			docs.append(d)
		await asyncio.gather(*(d.aio.insert() for d in docs))
		names = [d.name for d in docs]
		self.assertEqual(len(set(names)), 4)
		await asyncio.gather(*(d.aio.delete() for d in docs))

	async def test_async_controller_hook_under_doc_aio(self):
		"""THE deadlock case: doc.aio.insert holds the per-Database lock,
		the async validate hook awaits frappe.db.aio from another pool
		thread — dispatch_hook must release the lock for the duration."""
		seen = {}

		async def hook():
			seen["users"] = await frappe.db.aio.count("User")

		doc = await frappe.aio.new_doc("ToDo")
		doc.description = "phase16 async hook"
		doc.before_insert = hook
		await asyncio.wait_for(doc.aio.insert(), timeout=30)
		self.assertGreaterEqual(seen["users"], 2)
		await doc.aio.delete()


async def async_doc_event(doc, method):
	"""doc_events handler used by TestAioLifecycle (resolved by dotted path)."""
	doc.flags.aio_doc_event = await frappe.db.aio.count("User")


class TestAioLifecycle(AsyncIntegrationTestCase):
	"""Phase 17: submit/cancel via doc.aio, async doc_events hooks, and
	deadlock-free sync/async ORM mixing in one request."""

	async def test_submit_cancel_with_async_hook(self):
		from asgiref.sync import sync_to_async

		from frappe.core.doctype.doctype.test_doctype import new_doctype

		dt = await sync_to_async(lambda: new_doctype(is_submittable=1).insert(), thread_sensitive=False)()
		self.addAsyncCleanup(sync_to_async(dt.delete, thread_sensitive=False))

		seen = {}

		async def on_submit_hook():
			# nested facade-in-facade: doc.aio.submit holds the lock,
			# this hook takes it again from another pool thread
			seen["admin"] = await frappe.aio.get_value("User", "Administrator", "name")

		doc = await frappe.aio.new_doc(dt.name)
		doc.some_fieldname = "phase17"
		doc.on_submit = on_submit_hook
		await doc.aio.insert()
		await asyncio.wait_for(doc.aio.submit(), timeout=30)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(seen["admin"], "Administrator")

		await doc.aio.cancel()
		self.assertEqual(doc.docstatus, 2)
		await doc.aio.delete()

	async def test_async_doc_event_hook(self):
		self.addCleanup(setattr, frappe.local, "doc_events_hooks", None)
		with self.patch_hooks(
			{"doc_events": {"ToDo": {"after_insert": ["frappe.tests.test_aio_orm.async_doc_event"]}}}
		):
			frappe.local.doc_events_hooks = None  # bust the per-request cache
			doc = await frappe.aio.new_doc("ToDo")
			doc.description = "phase17 doc_event"
			await asyncio.wait_for(doc.aio.insert(), timeout=30)
			self.assertGreaterEqual(doc.flags.aio_doc_event, 2)
			await doc.aio.delete()

	async def test_nested_sync_async_mixing_no_deadlock(self):
		"""async facade -> async hook -> sync ORM -> async hook again —
		the worst realistic interleaving of both APIs in one request."""
		from asgiref.sync import sync_to_async

		seen = {}

		async def inner_hook():
			seen["count"] = await frappe.db.aio.count("User")

		def middle():
			d2 = frappe.new_doc("ToDo")
			d2.description = "phase17 inner"
			d2.before_insert = inner_hook
			d2.insert()
			d2.delete()

		async def outer_hook():
			await sync_to_async(middle, thread_sensitive=False)()

		doc = await frappe.aio.new_doc("ToDo")
		doc.description = "phase17 outer"
		doc.before_insert = outer_hook
		await asyncio.wait_for(doc.aio.insert(), timeout=30)
		self.assertGreaterEqual(seen["count"], 2)
		await doc.aio.delete()


class TestAsyncControllerSyncPath(IntegrationTestCase):
	"""Async controller methods also work on the plain sync ORM path
	(pool-thread requests, CLI) — dispatch_sync bridges them."""

	def test_async_controller_hook(self):
		seen = {}

		async def hook():
			seen["users"] = await frappe.db.aio.count("User")

		doc = frappe.new_doc("ToDo")
		doc.description = "phase16 sync path async hook"
		doc.before_insert = hook
		doc.insert()
		self.assertGreaterEqual(seen["users"], 2)
		doc.delete()

	def test_sync_controller_unchanged(self):
		doc = frappe.new_doc("ToDo")
		doc.description = "phase16 sync controller"
		doc.insert()  # ToDo's own sync controller hooks run as always
		self.assertTrue(doc.name)
		doc.delete()


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
