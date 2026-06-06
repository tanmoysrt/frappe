"""Phase 2: async-aware dispatch layer (frappe/dispatch.py).

All handlers are still sync in this phase — these tests prove the routing
primitives so async handlers (Phase 8) drop in without dispatch changes.
"""

import functools
import threading

from asgiref.sync import sync_to_async

import frappe
from frappe.dispatch import dispatch, dispatch_sync, is_async_callable
from frappe.tests import AsyncUnitTestCase, UnitTestCase


async def _async_fn():
	return "async"


def _sync_fn():
	return "sync"


@functools.wraps(_async_fn)
def _wrapped_async(*args, **kwargs):
	# mimics @frappe.whitelist(): sync @wraps wrapper returning the coroutine
	return _async_fn(*args, **kwargs)


class _AsyncCallable:
	async def __call__(self):
		return "call"


class TestIsAsyncCallable(UnitTestCase):
	def test_plain_functions(self):
		self.assertTrue(is_async_callable(_async_fn))
		self.assertFalse(is_async_callable(_sync_fn))

	def test_wraps_chain(self):
		self.assertTrue(is_async_callable(_wrapped_async))

	def test_partial(self):
		self.assertTrue(is_async_callable(functools.partial(_async_fn)))
		self.assertTrue(is_async_callable(functools.partial(functools.partial(_async_fn))))
		self.assertFalse(is_async_callable(functools.partial(_sync_fn)))

	def test_partial_of_wrapped(self):
		self.assertTrue(is_async_callable(functools.partial(_wrapped_async)))

	def test_callable_instance(self):
		self.assertTrue(is_async_callable(_AsyncCallable()))
		# the class itself is not async-callable (type.__call__ = constructor)
		self.assertFalse(is_async_callable(_AsyncCallable))

	def test_whitelisted_async_def(self):
		@frappe.whitelist(allow_guest=True)
		async def wl_method():
			return "wl"

		self.assertTrue(is_async_callable(wl_method))

	def test_whitelisted_sync_def(self):
		@frappe.whitelist(allow_guest=True)
		def wl_method():
			return "wl"

		self.assertFalse(is_async_callable(wl_method))


class TestDispatch(AsyncUnitTestCase):
	async def test_async_handler_runs_on_loop(self):
		loop_thread = threading.get_ident()
		seen = {}

		async def handler():
			seen["thread"] = threading.get_ident()
			return "ok"

		self.assertEqual(await dispatch(handler), "ok")
		self.assertEqual(seen["thread"], loop_thread)

	async def test_sync_handler_runs_in_pool(self):
		loop_thread = threading.get_ident()
		seen = {}

		def handler(x, y=0):
			seen["thread"] = threading.get_ident()
			return x + y

		self.assertEqual(await dispatch(handler, 1, y=2), 3)
		self.assertNotEqual(seen["thread"], loop_thread)

	async def test_wrapped_async_result_is_awaited(self):
		# sync @wraps wrapper hands back a coroutine — dispatch must await it
		self.assertEqual(await dispatch(_wrapped_async), "async")

	async def test_dispatch_sync_bridges_async_handler_from_pool_thread(self):
		"""Today's request path: pool thread hits an async handler ->
		async_to_sync routes it to the loop, thread blocks for the result."""
		loop_thread = threading.get_ident()
		seen = {}

		async def handler():
			seen["handler_thread"] = threading.get_ident()
			return "bridged"

		def request_path_in_pool():
			seen["pool_thread"] = threading.get_ident()
			return dispatch_sync(handler)

		result = await sync_to_async(request_path_in_pool, thread_sensitive=False)()
		self.assertEqual(result, "bridged")
		self.assertEqual(seen["handler_thread"], loop_thread)
		self.assertNotEqual(seen["pool_thread"], loop_thread)

	async def test_local_mutation_visible_across_dispatch_sync(self):
		"""frappe.local written by the async handler on the loop must be
		visible back in the pool thread afterwards (same ContextVar dict)."""

		async def handler():
			frappe.local.dispatch_key = "set-in-async"

		def request_path_in_pool():
			dispatch_sync(handler)
			return getattr(frappe.local, "dispatch_key", "<missing>")

		try:
			result = await sync_to_async(request_path_in_pool, thread_sensitive=False)()
			self.assertEqual(result, "set-in-async")
		finally:
			try:
				delattr(frappe.local, "dispatch_key")
			except AttributeError:
				pass


class TestDispatchSyncStandalone(UnitTestCase):
	"""No event loop anywhere (CLI / RQ worker context)."""

	def test_sync_handler_inline(self):
		seen = {}

		def handler():
			seen["thread"] = threading.get_ident()
			return "inline"

		self.assertEqual(dispatch_sync(handler), "inline")
		self.assertEqual(seen["thread"], threading.get_ident())

	def test_async_handler_one_shot_loop(self):
		async def handler():
			return "standalone"

		self.assertEqual(dispatch_sync(handler), "standalone")
