"""Phase 6: async DB on MariaDB (aiomysql, per-site pools).

Runs against the test site's own database with separately constructed
AsyncMariaDBDatabase instances — the site's normal frappe.db (pymysql or
async, per flag) is untouched. Covers: pool reuse, lazy single creation,
bounded acquire (503), rollback-on-release, session collation, idle evict
closing cold pools, shutdown.
"""

import time

import frappe
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
		frappe.conf.pop("db_pool_size", None)
		frappe.conf.pop("db_pool_acquire_timeout", None)

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
		frappe.conf.db_pool_size = 1
		frappe.conf.db_pool_acquire_timeout = 1
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

	def test_shutdown_pools_idempotent(self):
		self.db.sql("SELECT 1")
		self.db.close()
		aio.shutdown_pools()
		self.assertFalse(aio._reg()["pools"])
		aio.shutdown_pools()  # second call: no-op, no error
