"""Phase 3: async redis cache (frappe/utils/redis_wrapper.py + frappe/dispatch.py bridge).

Dual stack: sync callers keep the plain sync RedisWrapper (`frappe.cache`,
unchanged hot path); async callers use `frappe.cache.aio` — an
AsyncRedisWrapper (redis.asyncio) bound to the running loop. The bridge loop
in frappe.dispatch carries loop-bound async clients for sync/no-loop contexts
(the pattern the async DB phases will reuse): plain async_to_sync would spin a
one-shot loop per call and kill pooled connections on the second call.
"""

import asyncio
import threading

from asgiref.sync import sync_to_async

import unittest
from unittest.mock import patch

import frappe
from frappe.dispatch import get_bridge_loop, run_coroutine_sync
from frappe.tests import AsyncUnitTestCase, UnitTestCase
from frappe.utils.redis_wrapper import AsyncRedisWrapper, get_async_cache


class RedisBackendTestCase:
	"""Phase 21: frappe.cache defaults to the in-process backend — these
	tests exercise the OPT-IN redis backend, so they patch in a real
	RedisWrapper (skipping when no redis server is running)."""

	@classmethod
	def setUpClass(cls):
		from frappe.utils.redis_wrapper import setup_cache

		redis_cache = setup_cache()
		if not redis_cache.connected():
			raise unittest.SkipTest("redis_cache not running — redis-backend tests skipped")
		cls._redis_cache = redis_cache
		cls._cache_patch = patch.object(frappe, "cache", redis_cache)
		cls._cache_patch.start()
		super().setUpClass()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		cls._cache_patch.stop()


class TestAsyncCacheOnLoop(RedisBackendTestCase, AsyncUnitTestCase):
	async def test_aio_roundtrip(self):
		aio = frappe.cache.aio
		self.assertIsInstance(aio, AsyncRedisWrapper)
		await aio.set_value("phase3:aio", "v")
		self.assertEqual(await aio.get_value("phase3:aio"), "v")
		await aio.delete_value("phase3:aio")

	async def test_aio_client_is_per_loop(self):
		loop_client = frappe.cache.aio
		self.assertIs(frappe.cache.aio, loop_client)  # same loop → same client
		bridge_client = get_async_cache(get_bridge_loop())
		self.assertIsNot(loop_client, bridge_client)  # bridge loop → its own client

	async def test_local_cache_semantics(self):
		# aio keeps the request-local dict in sync, same as the sync client
		await frappe.cache.aio.set_value("phase3:aiolocal", "x")
		key = frappe.cache.make_key("phase3:aiolocal")
		self.assertEqual(frappe.local.cache.get(key), "x")
		# sync client sees the same key in redis (one redis, two clients)
		self.assertEqual(frappe.cache.get_value("phase3:aiolocal", use_local_cache=False), "x")
		await frappe.cache.aio.delete_value("phase3:aiolocal")

	async def test_async_generator_in_aio_get_value(self):
		await frappe.cache.aio.delete_value("phase3:agen")

		async def generator():
			return "async-generated"

		self.assertEqual(await frappe.cache.aio.get_value("phase3:agen", generator), "async-generated")
		await frappe.cache.aio.delete_value("phase3:agen")

	async def test_aio_hash_ops(self):
		aio = frappe.cache.aio
		await aio.hset("phase3:ahash", "k1", {"a": 1})
		self.assertEqual(await aio.hget("phase3:ahash", "k1"), {"a": 1})
		self.assertTrue(await aio.hexists("phase3:ahash", "k1"))
		await aio.hdel("phase3:ahash", "k1")
		self.assertFalse(await aio.hexists("phase3:ahash", "k1"))

	async def test_run_coroutine_sync_fails_fast_on_loop_thread(self):
		# blocking the loop on the bridge would stall the whole process
		with self.assertRaises(RuntimeError):
			run_coroutine_sync(asyncio.sleep(0))


class TestBridgeLoopNoLoop(RedisBackendTestCase, UnitTestCase):
	"""No event loop in the calling thread (CLI / bench / patches) — the
	pattern the async DB phases will rely on."""

	def _bridge_client(self):
		return get_async_cache(get_bridge_loop())

	def test_repeated_calls_are_stable(self):
		# regression: async_to_sync's one-shot loops broke the connection
		# pool on every second call ("Event loop is closed")
		client = self._bridge_client()
		for i in range(5):
			run_coroutine_sync(client.set_value("phase3:stable", i))
			self.assertEqual(run_coroutine_sync(client.get_value("phase3:stable")), i)
		run_coroutine_sync(client.delete_value("phase3:stable"))

	def test_bridge_loop_is_persistent(self):
		loop1 = get_bridge_loop()
		run_coroutine_sync(self._bridge_client().set_value("phase3:bridge", 1))
		loop2 = get_bridge_loop()
		self.assertIs(loop1, loop2)
		self.assertTrue(loop2.is_running())
		run_coroutine_sync(self._bridge_client().delete_value("phase3:bridge"))

	def test_local_context_propagates_to_bridge(self):
		# make_key reads frappe.local.conf on the bridge loop — caller's
		# context must be visible there (call_soon_threadsafe captures it)
		run_coroutine_sync(self._bridge_client().set_value("phase3:ctx", "v"))
		key = frappe.cache.make_key("phase3:ctx")
		# mutation made on the bridge loop is visible back in this thread
		self.assertEqual(frappe.local.cache.get(key), "v")
		# and the key carries this site's db_name prefix
		self.assertEqual(frappe.cache.get_value("phase3:ctx", use_local_cache=False), "v")
		run_coroutine_sync(self._bridge_client().delete_value("phase3:ctx"))


class TestSyncClientUnchanged(RedisBackendTestCase, AsyncUnitTestCase):
	"""Sync frappe.cache stays the plain sync client — and works from pool
	threads exactly as before (server topology)."""

	async def test_sync_cache_from_pool_thread(self):
		loop_thread = threading.get_ident()

		def request_path():
			self.assertNotEqual(threading.get_ident(), loop_thread)
			frappe.cache.set_value("phase3:pool", "from-pool")
			return frappe.cache.get_value("phase3:pool")

		result = await sync_to_async(request_path, thread_sensitive=False)()
		self.assertEqual(result, "from-pool")
		await frappe.cache.aio.delete_value("phase3:pool")
