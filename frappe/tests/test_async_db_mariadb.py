"""Phase 6: async DB on MariaDB (aiomysql, per-site pools).

Runs against the test site's own database with separately constructed
AsyncMariaDBDatabase instances — the site's normal frappe.db (pymysql or
async, per flag) is untouched. Covers: pool reuse, lazy single creation,
bounded acquire (503), rollback-on-release, session collation, idle evict
closing cold pools, shutdown.
"""

import time

import frappe
from frappe.config import patch_common_conf
from frappe.database.mariadb import aio
from frappe.dispatch import run_coroutine_sync
from frappe.tests import IntegrationTestCase


def make_db():
	conf = frappe.conf
	return aio.AsyncMariaDBDatabase(
		socket=conf.db_socket,
		host=conf.db_host or "127.0.0.1",
		user=conf.db_user or conf.db_name,
		password=conf.db_password,
		port=conf.db_port,
		cur_db_name=conf.db_name,
	)


class TestAsyncMariaDB(IntegrationTestCase):
	def setUp(self):
		# the site itself may run on the async backend (use_async_db) — the
		# runner's frappe.db then holds a pooled conn that shutdown_pools
		# would terminate under it. Release it first; it reconnects lazily.
		frappe.db.close()
		# fresh pool world per test — pool sizing/timeout conf is snapshotted
		# at creation, so leftovers from other tests would leak settings
		aio.shutdown_pools()
		self.db = make_db()

	def tearDown(self):
		self.db.close()
		aio.shutdown_pools()

	def test_basic_sql(self):
		self.db.sql("SELECT 1")
		rows = self.db.sql("SELECT name FROM tabUser WHERE name=%s", ("Administrator",), as_dict=True)
		self.assertEqual(rows[0].name, "Administrator")
		self.assertEqual(self.db.get_value("User", "Administrator", "name"), "Administrator")

	def test_session_collation_applied(self):
		# no collation kwarg in aiomysql — re-applied per acquire
		self.assertEqual(self.db.sql("SELECT @@collation_connection", pluck=True)[0], "utf8mb4_unicode_ci")

	def test_pool_reuse_across_connections(self):
		self.db.sql("SELECT 1")
		key = self.db._pool_key()
		pool = aio._reg()["pools"][key]
		self.db.close()  # rollback + release, NOT a TCP close
		self.assertEqual(pool.freesize, 1)

		db2 = make_db()
		db2.sql("SELECT 1")
		self.assertIs(aio._reg()["pools"][key], pool)  # same pool, lazily created once
		self.assertEqual(pool.size, 1)  # conn reused, not a second one
		db2.close()

	def test_acquire_timeout_raises_503(self):
		# Phase 19: pool knobs come from common config only
		self.enterContext(patch_common_conf(db_pool_size=1, db_pool_acquire_timeout=1))
		self.db.sql("SELECT 1")  # holds the only connection
		db2 = make_db()
		with self.assertRaises(aio.PoolAcquireTimeoutError) as ctx:
			db2.sql("SELECT 1")
		self.assertEqual(ctx.exception.http_status_code, 503)

	def test_rollback_on_release(self):
		self.db.sql(
			"INSERT INTO `tabToDo` (name, description, modified, creation) "
			"VALUES ('pool-dirty-test', 'x', NOW(), NOW())"
		)
		self.db.close()  # uncommitted write must die with the release
		db2 = make_db()
		self.assertFalse(db2.sql("SELECT name FROM `tabToDo` WHERE name='pool-dirty-test'"))
		db2.close()

	def test_idle_evict_closes_cold_pool(self):
		self.db.sql("SELECT 1")
		self.db.close()
		reg = aio._reg()
		key = self.db._pool_key()
		pool = reg["pools"][key]
		reg["last_used"][key] = time.monotonic() - 10_000  # fake cold site
		run_coroutine_sync(aio._evict_idle_pools_once())
		self.assertNotIn(key, reg["pools"])
		self.assertTrue(pool._closed)  # conns + buffers freed, not idled

	def test_evict_skips_pool_in_use(self):
		self.db.sql("SELECT 1")  # connection held — must survive the sweep
		reg = aio._reg()
		key = self.db._pool_key()
		reg["last_used"][key] = time.monotonic() - 10_000
		run_coroutine_sync(aio._evict_idle_pools_once())
		self.assertIn(key, reg["pools"])
		self.assertEqual(self.db.sql("SELECT 1")[0][0], 1)  # still usable

	def test_transactions_and_savepoints(self):
		self.db.sql(
			"INSERT INTO `tabToDo` (name, description, modified, creation) "
			"VALUES ('pool-txn-test', 'keep', NOW(), NOW())"
		)
		self.db.savepoint("sp_pool")
		self.db.sql("UPDATE `tabToDo` SET description='drop' WHERE name='pool-txn-test'")
		self.db.rollback(save_point="sp_pool")
		desc = self.db.sql("SELECT description FROM `tabToDo` WHERE name='pool-txn-test'", pluck=True)
		self.assertEqual(desc, ["keep"])
		self.db.rollback()  # leave no trace

	def test_fork_safety(self):
		"""RQ-worker shape: fork after pools exist. Child must get a fresh
		registry and work; parent's conn must survive child GC of inherited
		driver objects (the shared-epoll trap: child-side transport.close()
		would epoll_ctl(DEL) the parent's reader subscription)."""
		import subprocess
		import sys
		import textwrap

		script = textwrap.dedent(f"""
			import os, gc
			import frappe
			frappe.init({frappe.local.site!r})
			frappe.connect()
			assert frappe.db.get_value("User", "Administrator", "name") == "Administrator"
			pid = os.fork()
			if pid == 0:
				frappe.local.db = None
				gc.collect()  # worst case: child GCs every inherited driver object
				frappe.connect()
				assert frappe.db.get_value("User", "Guest", "name") == "Guest"
				frappe.destroy()
				os._exit(0)
			_, status = os.waitpid(pid, 0)
			assert os.waitstatus_to_exitcode(status) == 0, "child failed"
			# the parent's connection must still be readable after the child dies
			assert frappe.db.get_value("User", "Administrator", "name") == "Administrator"
			frappe.destroy()
			print("fork-ok")
		""")
		result = subprocess.run(
			[sys.executable, "-c", script],
			capture_output=True,
			text=True,
			timeout=60,
			cwd=frappe.utils.get_bench_path() + "/sites",
		)
		self.assertEqual(result.returncode, 0, msg=result.stderr[-2000:])
		self.assertIn("fork-ok", result.stdout)

	def test_shutdown_pools_idempotent(self):
		self.db.sql("SELECT 1")
		self.db.close()
		aio.shutdown_pools()
		self.assertFalse(aio._reg()["pools"])
		aio.shutdown_pools()  # second call: no-op, no error

	def test_gc_collect_safe_does_not_wedge_bridge_loop(self):
		"""Phase 26 regression: aiomysql transport teardown (Connection ->
		StreamWriter -> transport -> loop._remove_reader/_remove_writer) mutates
		the bridge loop's selector, which is not thread-safe. A bare
		``gc.collect()`` from any non-bridge thread (the server's 30s idle-trim
		ran on the main uvicorn loop) corrupts the selector mid-I/O and hangs
		every DB call. ``frappe.dispatch.gc_collect_safe`` confines the collect
		(and its finalizers) to the bridge-loop thread. Under concurrent queries
		plus a thread hammering gc_collect_safe, all work must finish — a wedged
		loop would leave worker threads alive past the deadline."""
		import asyncio
		import threading
		import time

		from frappe.dispatch import gc_collect_safe, get_bridge_loop

		self.db.sql("SELECT 1")  # ensure a pool + the bridge loop exist
		loop = get_bridge_loop()
		site = frappe.local.site
		stop = threading.Event()
		errors = []

		def worker():
			# each thread is its own "request": fresh frappe.local + pooled conn,
			# released on destroy (mirrors the ASGI per-request lifecycle)
			try:
				for _ in range(40):
					frappe.init(site, force=True)
					try:
						frappe.connect()
						frappe.get_all("User", fields=["name"], limit=5)
						frappe.db.commit()
					finally:
						frappe.destroy()
			except Exception as e:  # noqa: BLE001
				errors.append(repr(e))

		def gc_driver():
			# the real fix, driven from the bridge loop: gc_collect_safe sees
			# get_running_loop()==bridge and collects loop-local
			while not stop.is_set():
				try:
					asyncio.run_coroutine_threadsafe(gc_collect_safe(), loop).result(timeout=10)
				except Exception as e:  # noqa: BLE001
					errors.append(f"gc_collect_safe: {e!r}")
					return

		# daemon threads: if the fix ever regresses and the loop wedges, the test
		# fails on the deadline instead of hanging the whole runner
		workers = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
		gct = threading.Thread(target=gc_driver, daemon=True)
		gct.start()
		for t in workers:
			t.start()
		end = time.monotonic() + 30
		for t in workers:
			t.join(timeout=max(0, end - time.monotonic()))
		stop.set()

		alive = [t.name for t in workers if t.is_alive()]
		self.assertFalse(alive, f"bridge loop wedged — workers stuck on DB I/O: {alive}")
		self.assertFalse(errors, f"async DB errored under concurrent gc_collect_safe: {errors}")
