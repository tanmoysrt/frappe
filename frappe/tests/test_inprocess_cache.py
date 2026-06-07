"""Phase 21: in-process cache backend (frappe/utils/inprocess_cache.py).

Semantics mirror RedisWrapper: pickled values, db_name key prefixing,
frappe.local.cache interplay, TTL, FIFO bound, bump-file invalidation.
"""

import os
import tempfile
import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.inprocess_cache import InProcessCache, InProcessClientCache


class TestInProcessCache(IntegrationTestCase):
	def setUp(self):
		self.cache = InProcessCache()
		# isolate from the request-local memo layer
		frappe.local.cache = {}

	def test_value_roundtrip_and_isolation(self):
		original = {"a": [1, 2, 3]}
		self.cache.set_value("ipc:key", original)
		frappe.local.cache = {}  # force store read
		value = self.cache.get_value("ipc:key")
		self.assertEqual(value, original)
		# pickled snapshot: mutating the stored copy can't leak back
		value["a"].append(4)
		frappe.local.cache = {}
		self.assertEqual(self.cache.get_value("ipc:key"), {"a": [1, 2, 3]})

	def test_local_cache_memo(self):
		self.cache.set_value("ipc:memo", "x")
		key = self.cache.make_key("ipc:memo")
		self.assertEqual(frappe.local.cache[key], "x")
		self.assertEqual(self.cache.get_value("ipc:memo"), "x")

	def test_generator(self):
		calls = []

		def gen():
			calls.append(1)
			return "generated"

		self.assertEqual(self.cache.get_value("ipc:gen", generator=gen), "generated")
		self.assertEqual(self.cache.get_value("ipc:gen", generator=gen), "generated")
		self.assertEqual(len(calls), 1)

	def test_expiry(self):
		self.cache.set_value("ipc:ttl", "v", expires_in_sec=1000)
		frappe.local.cache = {}
		self.assertEqual(self.cache.get_value("ipc:ttl", expires=True), "v")
		# force expiry
		key = self.cache.make_key("ipc:ttl")
		self.cache._expiries[key if isinstance(key, bytes) else key.encode()] = time.monotonic() - 1
		frappe.local.cache = {}
		self.assertIsNone(self.cache.get_value("ipc:ttl", expires=True))

	def test_delete_and_keys(self):
		self.cache.set_value("ipc:del:a", 1)
		self.cache.set_value("ipc:del:b", 2)
		keys = self.cache.get_keys("ipc:del")
		self.assertEqual(len(keys), 2)
		self.cache.delete_keys("ipc:del")
		frappe.local.cache = {}
		self.assertIsNone(self.cache.get_value("ipc:del:a"))
		self.assertEqual(self.cache.get_keys("ipc:del"), [])

	def test_hash_ops(self):
		self.cache.hset("ipc:h", "f1", {"deep": True})
		frappe.local.cache = {}
		self.assertEqual(self.cache.hget("ipc:h", "f1"), {"deep": True})
		self.assertTrue(self.cache.hexists("ipc:h", "f1"))
		self.assertEqual(self.cache.hgetall("ipc:h"), {b"f1": {"deep": True}})
		self.assertEqual(self.cache.hkeys("ipc:h"), [b"f1"])
		self.cache.hdel("ipc:h", "f1")
		frappe.local.cache = {}
		self.assertIsNone(self.cache.hget("ipc:h", "f1"))

	def test_hget_generator(self):
		value = self.cache.hget("ipc:hg", "k", generator=lambda: 42)
		self.assertEqual(value, 42)
		frappe.local.cache = {}
		self.assertEqual(self.cache.hget("ipc:hg", "k"), 42)

	def test_set_ops(self):
		self.cache.sadd("ipc:s", "a", "b")
		self.assertTrue(self.cache.sismember("ipc:s", "a"))
		self.assertFalse(self.cache.sismember("ipc:s", "z"))
		self.assertEqual(self.cache.smembers("ipc:s"), {b"a", b"b"})
		self.cache.srem("ipc:s", "a")
		self.assertFalse(self.cache.sismember("ipc:s", "a"))

	def test_list_ops(self):
		self.cache.rpush("ipc:l", "one")
		self.cache.rpush("ipc:l", "two")
		self.cache.lpush("ipc:l", "zero")
		self.assertEqual(self.cache.llen("ipc:l"), 3)
		self.assertEqual(self.cache.lrange("ipc:l", 0, -1), [b"zero", b"one", b"two"])
		self.assertEqual(self.cache.lindex("ipc:l", 1), b"one")
		self.assertEqual(self.cache.lpop("ipc:l"), b"zero")
		self.assertEqual(self.cache.rpop("ipc:l"), b"two")
		self.cache.ltrim("ipc:l", 0, -1)

	def test_blpop(self):
		self.cache.lpush("ipc:bl", "tok")
		result = self.cache.blpop("ipc:bl", timeout=1)
		self.assertEqual(result[1], b"tok")
		self.assertIsNone(self.cache.blpop("ipc:bl", timeout=0.05))

	def test_counters_raw_keys(self):
		key = self.cache.make_key("ipc:counter")
		self.assertEqual(self.cache.incrby(key, 5), 5)
		self.assertEqual(self.cache.incrby(key, 2), 7)
		self.assertEqual(int(self.cache.get(key)), 7)
		self.cache.expire(key, 1000)
		self.assertGreater(self.cache.ttl(key), 0)
		self.cache.setex(key, 1000, 0)
		self.assertEqual(int(self.cache.get(key)), 0)

	def test_pipeline(self):
		pipeline = self.cache.pipeline()
		pipeline.set("ipc:p1", "a", 100)
		pipeline.set("ipc:p2", "b", 100)
		pipeline.execute()
		self.assertEqual(self.cache.get("ipc:p1"), b"a")

	def test_exists(self):
		self.cache.set_value("ipc:e", 1)
		self.assertEqual(self.cache.exists("ipc:e"), 1)
		self.assertEqual(self.cache.exists("ipc:nope"), 0)

	def test_fifo_eviction(self):
		small = InProcessCache(max_entries=10)
		for i in range(15):
			small.set(f"k{i}", b"v")
		self.assertLessEqual(len(small._strings), 11)
		self.assertNotIn(b"k0", small._strings)  # oldest evicted
		self.assertIn(b"k14", small._strings)

	def test_flushall(self):
		self.cache.set_value("ipc:f", 1)
		self.cache.flushall()
		frappe.local.cache = {}
		self.assertIsNone(self.cache.get_value("ipc:f"))

	def test_bump_file_invalidation(self):
		tmp = tempfile.mkdtemp()
		with patch.object(frappe.local, "sites_path", tmp):
			self.cache.set_value("ipc:gen2", "warm")
			self.cache._generation_checked = 0  # allow immediate stat
			self.cache.check_generation()  # baseline (file absent -> noop)
			InProcessCache.bump_generation()
			self.cache._generation_checked = 0
			self.cache.check_generation()  # records baseline mtime
			frappe.local.cache = {}
			self.assertEqual(self.cache.get_value("ipc:gen2"), "warm")  # no flush on first sight
			time.sleep(0.01)
			InProcessCache.bump_generation()
			self.cache._generation_checked = 0
			self.cache.check_generation()
			frappe.local.cache = {}
			self.assertIsNone(self.cache.get_value("ipc:gen2"))  # flushed

	def test_statistics(self):
		self.cache.set_value("ipc:st", 1)
		frappe.local.cache = {}
		self.cache.get_value("ipc:st")
		stats = self.cache.statistics
		self.assertTrue(stats.healthy)
		self.assertGreaterEqual(stats.hits, 1)

	def test_aio_facade(self):
		import asyncio

		async def run():
			await self.cache.aio.set_value("ipc:aio", 99)
			frappe.local.cache = {}
			return await self.cache.aio.get_value("ipc:aio")

		self.assertEqual(asyncio.run(run()), 99)


class TestInProcessClientCache(IntegrationTestCase):
	def setUp(self):
		self.backend = InProcessCache()
		self.client = InProcessClientCache(self.backend)
		frappe.local.cache = {}

	def test_roundtrip_object_identity(self):
		doc = {"big": "object"}
		self.client.set_value("icc:k", doc)
		self.assertIs(self.client.get_value("icc:k"), doc)  # memoized, not re-pickled

	def test_generator_and_backend_passthrough(self):
		value = self.client.get_value("icc:gen", generator=lambda: [1, 2])
		self.assertEqual(value, [1, 2])
		self.assertIs(self.client.get_value("icc:gen"), value)

	def test_delete(self):
		self.client.set_value("icc:d", 5)
		self.client.delete_value("icc:d")
		frappe.local.cache = {}
		self.assertIsNone(self.client.get_value("icc:d"))

	def test_maxsize_fifo(self):
		small = InProcessClientCache(self.backend, maxsize=5)
		for i in range(10):
			small.set_value(f"icc:m{i}", i)
		self.assertLessEqual(len(small.cache), 6)

	def test_statistics(self):
		self.client.set_value("icc:s", 1)
		self.client.get_value("icc:s")
		self.assertGreaterEqual(self.client.statistics.hits, 1)
		self.client.reset_statistics()
		self.assertEqual(self.client.statistics.hits, 0)
