"""Phase 13: python socket.io server — auth + permission helpers.

The socket plumbing itself (engine.io, rooms, redis subscriber) is verified
live against the booted server; these cover the frappe-touching pieces the
connect/subscribe handlers run in pool threads. The helpers do their own
frappe.init/destroy, so each call runs in a private contextvars Context —
exactly like production (and so they can't destroy the runner's context).
"""

import contextvars
import threading

import frappe
from frappe import realtime_server
from frappe.tests import IntegrationTestCase


def _call_clean(fn, *args):
	"""Run fn in a thread with an EMPTY contextvars Context (Python 3.14
	threads inherit a context copy whose frappe.local dict is shared —
	fn's frappe.destroy would tear down the test runner's site otherwise)."""
	result = {}

	def runner():
		try:
			result["value"] = fn(*args)
		except Exception as e:
			result["error"] = e

	thread = threading.Thread(target=contextvars.Context().run, args=(runner,))
	thread.start()
	thread.join()
	if "error" in result:
		raise result["error"]
	return result["value"]


class TestRealtimeHelpers(IntegrationTestCase):
	def test_hostname(self):
		for given, expected in [
			("http://test.local:8001", "test.local"),
			("https://Test.Local", "test.local"),
			("test.local:9001", "test.local"),
			("http://test.local:8001/app/x", "test.local"),
			(None, None),
			("", None),
		]:
			self.assertEqual(realtime_server._hostname(given), expected)

	def test_resolve_user_via_api_key(self):
		user = frappe.get_doc("User", "Administrator")
		api_key = frappe.generate_hash(length=15)
		api_secret = frappe.generate_hash(length=15)
		original_key = user.api_key
		user.api_key = api_key
		user.api_secret = api_secret
		user.save(ignore_permissions=True)
		frappe.db.commit()  # _resolve_user opens its own connection
		self.addCleanup(self._restore_api_key, original_key)

		info = _call_clean(
			realtime_server._resolve_user, frappe.local.site, None, f"token {api_key}:{api_secret}"
		)
		self.assertEqual(info["user"], "Administrator")
		self.assertEqual(info["user_type"], "System User")

	def _restore_api_key(self, original_key):
		frappe.db.set_value("User", "Administrator", "api_key", original_key)
		frappe.db.commit()

	def test_resolve_user_with_bogus_sid_is_guest(self):
		info = _call_clean(realtime_server._resolve_user, frappe.local.site, "sid=bogus-sid-value", None)
		self.assertEqual(info["user"], "Guest")

	def test_check_permission(self):
		site = frappe.local.site
		self.assertTrue(_call_clean(realtime_server._check_permission, site, "Administrator", "User", None))
		self.assertFalse(_call_clean(realtime_server._check_permission, site, "Guest", "User", None))
		self.assertFalse(
			_call_clean(realtime_server._check_permission, site, "Guest", "No Such Doctype", None)
		)
