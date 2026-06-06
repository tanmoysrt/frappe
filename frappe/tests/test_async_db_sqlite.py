"""Phase 5: async DB on SQLite (aiosqlite) — proving ground.

The async backend keeps the whole Database class unchanged: driver I/O is
aiosqlite living on the bridge loop, reached through the sync adapters in
frappe.database.aio. These tests run the real Database code paths
(sql/commit/rollback/savepoint/hooks) against a throwaway sqlite file, plus
the awaitable facade (``db.aio``) used by async handlers from Phase 8 on.
"""

import shutil
import tempfile
from pathlib import Path

from frappe.database.sqlite.aio import AsyncSQLiteDatabase
from frappe.tests import AsyncUnitTestCase, UnitTestCase


class _TempSQLiteDB(AsyncSQLiteDatabase):
	"""AsyncSQLiteDatabase pointed at a temp file instead of the site dir."""

	def __init__(self, path):
		self._db_path = path
		super().__init__(cur_db_name="test_aio")

	def get_db_path(self):
		return self._db_path


def make_db(tmpdir):
	db = _TempSQLiteDB(Path(tmpdir) / "test_aio.db")
	db.sql_ddl("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
	return db


class TestAsyncSQLiteDatabase(UnitTestCase):
	def setUp(self):
		self.tmpdir = tempfile.mkdtemp()
		self.db = make_db(self.tmpdir)

	def tearDown(self):
		self.db.close()
		shutil.rmtree(self.tmpdir, ignore_errors=True)

	def test_crud_round_trip(self):
		self.db.sql("INSERT INTO kv (k, v) VALUES (%s, %s)", ("a", "1"))
		self.db.sql("INSERT INTO kv (k, v) VALUES (%(k)s, %(v)s)", {"k": "b", "v": "2"})
		rows = self.db.sql("SELECT k, v FROM kv ORDER BY k", as_dict=True)
		self.assertEqual(rows, [{"k": "a", "v": "1"}, {"k": "b", "v": "2"}])
		self.assertEqual(self.db.sql("SELECT k FROM kv ORDER BY k", pluck=True), ["a", "b"])

	def test_pragmas_applied(self):
		self.db.sql("SELECT 1")  # force connect
		self.assertEqual(self.db.sql("PRAGMA journal_mode")[0][0], "wal")
		self.assertEqual(self.db.sql("PRAGMA cache_size")[0][0], -2048)
		self.assertEqual(self.db.sql("PRAGMA mmap_size")[0][0], 0)

	def test_commit_and_rollback(self):
		self.db.sql("INSERT INTO kv (k, v) VALUES ('keep', 'x')")
		self.db.commit()
		self.db.sql("INSERT INTO kv (k, v) VALUES ('drop', 'y')")
		self.db.rollback()
		self.assertEqual(self.db.sql("SELECT k FROM kv", pluck=True), ["keep"])

	def test_commit_rollback_hooks(self):
		ran = []
		self.db.after_commit.add(lambda: ran.append("commit"))
		self.db.commit()
		self.db.after_rollback.add(lambda: ran.append("rollback"))
		self.db.rollback()
		self.assertEqual(ran, ["commit", "rollback"])

	def test_savepoint_partial_rollback(self):
		self.db.sql("INSERT INTO kv (k, v) VALUES ('before', 'x')")
		self.db.savepoint("sp1")
		self.db.sql("INSERT INTO kv (k, v) VALUES ('after', 'y')")
		self.db.rollback(save_point="sp1")
		self.assertEqual(self.db.sql("SELECT k FROM kv", pluck=True), ["before"])

	def test_read_only_connection_swap(self):
		# begin(read_only=True) closes the rw connection and reopens mode=ro —
		# exercises adapter close/cursor recreation end to end
		self.db.sql("INSERT INTO kv (k, v) VALUES ('ro', 'x')")
		self.db.commit()
		self.db.begin(read_only=True)
		self.assertEqual(self.db.sql("SELECT v FROM kv WHERE k='ro'", pluck=True), ["x"])
		self.db.begin()  # back to read-write
		self.db.sql("INSERT INTO kv (k, v) VALUES ('rw', 'y')")
		self.assertEqual(len(self.db.sql("SELECT k FROM kv")), 2)

	def test_close_and_reconnect(self):
		self.db.sql("SELECT 1")
		self.db.close()
		self.assertIsNone(self.db._conn)
		self.assertEqual(self.db.sql("SELECT 1")[0][0], 1)  # lazy reconnect


class TestAsyncSQLiteFacade(AsyncUnitTestCase):
	"""``await db.aio.<method>(...)`` — the loop never blocks on DB I/O."""

	def setUp(self):
		self.tmpdir = tempfile.mkdtemp()
		self.db = make_db(self.tmpdir)

	def tearDown(self):
		self.db.close()
		shutil.rmtree(self.tmpdir, ignore_errors=True)

	async def test_awaitable_sql(self):
		await self.db.aio.sql("INSERT INTO kv (k, v) VALUES (%s, %s)", ("x", "42"))
		rows = await self.db.aio.sql("SELECT v FROM kv WHERE k=%s", ("x",), pluck=True)
		self.assertEqual(rows, ["42"])

	async def test_awaitable_commit(self):
		await self.db.aio.sql("INSERT INTO kv (k, v) VALUES ('c', '1')")
		await self.db.aio.commit()
		await self.db.aio.rollback()
		self.assertEqual(await self.db.aio.sql("SELECT k FROM kv", pluck=True), ["c"])

	async def test_non_callable_passthrough(self):
		self.assertEqual(self.db.aio.db_type, "sqlite")
