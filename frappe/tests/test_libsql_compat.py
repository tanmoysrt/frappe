"""Phase 22: libsql_compat — the sqlite3-shaped adapter over libsql.

Pins the engine-parity contract: exception classes, Row access, ISO type
conversion, RETURNING, WAL live-file interop with stdlib sqlite3, and the
aiosqlite-shaped async runner.
"""

import asyncio
import os
import sqlite3
import tempfile
from datetime import date, datetime, time

from frappe.database.sqlite import libsql_compat
from frappe.tests import UnitTestCase


class TestLibsqlCompat(UnitTestCase):
	def setUp(self):
		self.dir = tempfile.mkdtemp()
		self.path = os.path.join(self.dir, "t.db")
		self.conn = libsql_compat.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES)
		self.conn.execute(
			"CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT UNIQUE, ts timestamp, d date, tm time)"
		)
		self.conn.commit()

	def tearDown(self):
		self.conn.close()

	def test_roundtrip_and_lastrowid(self):
		cur = self.conn.cursor()
		cur.execute("INSERT INTO t (b) VALUES (?)", ("x",))
		self.assertEqual(cur.lastrowid, 1)
		self.assertEqual(cur.rowcount, 1)
		self.conn.commit()
		cur.execute("SELECT a, b FROM t")
		self.assertEqual(cur.fetchall(), [(1, "x")])

	def test_exception_mapping(self):
		self.conn.execute("INSERT INTO t (b) VALUES ('dup')")
		with self.assertRaises(sqlite3.IntegrityError):
			self.conn.execute("INSERT INTO t (b) VALUES ('dup')")
		self.conn.rollback()
		with self.assertRaises(sqlite3.OperationalError):
			self.conn.execute("SELECT * FROM no_such_table")
		with self.assertRaises(sqlite3.ProgrammingError):
			self.conn.execute("SELEC syntax error")
		# all map under sqlite3.Error for blanket handlers
		with self.assertRaises(sqlite3.Error):
			self.conn.execute("SELECT * FROM no_such_table")

	def test_row_factory(self):
		self.conn.row_factory = sqlite3.Row
		self.conn.execute("INSERT INTO t (b) VALUES ('named')")
		row = self.conn.execute("SELECT a, b FROM t WHERE b='named'").fetchone()
		self.assertEqual(row["b"], "named")
		self.assertEqual(row[1], "named")
		self.assertEqual(row.keys(), ["a", "b"])
		self.assertEqual(len(row), 2)
		self.conn.rollback()

	def test_iso_conversion(self):
		self.conn.execute(
			"INSERT INTO t (b, ts, d, tm) VALUES ('typed', '2026-01-01 10:30:00', '2026-01-01', '10:30:00')"
		)
		ts, d, tm = self.conn.execute("SELECT ts, d, tm FROM t WHERE b='typed'").fetchone()
		self.assertEqual(ts, datetime(2026, 1, 1, 10, 30))
		self.assertEqual(d, date(2026, 1, 1))
		self.assertEqual(tm, time(10, 30))
		# microseconds variant
		self.conn.execute("INSERT INTO t (b, ts) VALUES ('us', '2026-01-01 10:30:00.123456')")
		(ts2,) = self.conn.execute("SELECT ts FROM t WHERE b='us'").fetchone()
		self.assertEqual(ts2.microsecond, 123456)
		# untyped (TEXT) columns stay str even when date-shaped — decltype-driven
		self.conn.execute("INSERT INTO t (b) VALUES ('2026-01-01')")
		(b,) = self.conn.execute("SELECT b FROM t WHERE a=(SELECT max(a) FROM t)").fetchone()
		self.assertIsInstance(b, str)
		# date-only value in a TIMESTAMP column converts to datetime (stdlib parity)
		self.conn.execute("INSERT INTO t (b, ts) VALUES ('dateonly', '2014-01-01')")
		(ts3,) = self.conn.execute("SELECT ts FROM t WHERE b='dateonly'").fetchone()
		self.assertEqual(ts3, datetime(2014, 1, 1))
		self.conn.rollback()

	def test_no_conversion_without_detect_types(self):
		plain = libsql_compat.connect(self.path)
		plain.execute("INSERT INTO t (b, ts) VALUES ('plainconn', '2026-01-01 10:30:00')")
		(ts,) = plain.execute("SELECT ts FROM t WHERE b='plainconn'").fetchone()
		self.assertIsInstance(ts, str)  # queue/search parity: detect_types off
		plain.rollback()
		plain.close()

	def test_native_regexp(self):
		(result,) = self.conn.execute("SELECT 'frappe' REGEXP 'fra.+'").fetchone()
		self.assertEqual(result, 1)
		# registering regexp is a harmless no-op (native)
		self.conn.create_function("regexp", 2, lambda a, b: None)

	def test_returning(self):
		self.conn.execute("INSERT INTO t (b) VALUES ('claim')")
		rows = self.conn.execute("UPDATE t SET b='claimed' WHERE b='claim' RETURNING a, b").fetchall()
		self.assertEqual(rows[0][1], "claimed")
		self.conn.rollback()

	def test_executemany_and_iteration(self):
		cur = self.conn.cursor()
		cur.executemany("INSERT INTO t (b) VALUES (?)", [("i1",), ("i2",)])
		cur.execute("SELECT b FROM t ORDER BY a")
		self.assertEqual([r[0] for r in cur], ["i1", "i2"])
		self.conn.rollback()

	def test_executescript_with_pragmas(self):
		# the queue connects via executescript(PRAGMAs + SCHEMA) — must work
		self.conn.executescript(
			"PRAGMA busy_timeout=5000;\nCREATE TABLE IF NOT EXISTS s (x INTEGER);\n"
		)
		self.conn.execute("INSERT INTO s VALUES (1)")
		self.conn.commit()

	def test_wal_live_interop_with_stdlib(self):
		cur = self.conn.cursor()
		cur.execute("PRAGMA journal_mode=WAL")
		self.assertEqual(cur.fetchone()[0], "wal")
		self.conn.execute("INSERT INTO t (b) VALUES ('interop')")
		self.conn.commit()
		# stdlib reads the live libsql WAL file
		s = sqlite3.connect(self.path, timeout=5)
		self.assertEqual(
			s.execute("SELECT count(*) FROM t WHERE b='interop'").fetchone()[0], 1
		)
		# and libsql sees a stdlib write immediately
		s.execute("INSERT INTO t (b) VALUES ('from-stdlib')")
		s.commit()
		s.close()
		count = self.conn.execute("SELECT count(*) FROM t WHERE b='from-stdlib'").fetchone()[0]
		self.assertEqual(count, 1)

	def test_read_only(self):
		self.conn.execute("INSERT INTO t (b) VALUES ('ro')")
		self.conn.commit()
		ro = libsql_compat.connect(f"file:{self.path}?mode=ro")
		self.assertEqual(ro.execute("SELECT count(*) FROM t").fetchone()[0], 1)
		with self.assertRaises(sqlite3.Error):
			ro.execute("INSERT INTO t (b) VALUES ('nope')")
			ro.commit()
		ro.close()

	def test_isolation_none_autocommit(self):
		auto = libsql_compat.connect(self.path, isolation_level=None)
		auto.execute("INSERT INTO t (b) VALUES ('auto')")
		# visible to another connection WITHOUT commit (autocommit)
		other = sqlite3.connect(self.path, timeout=5)
		self.assertEqual(other.execute("SELECT count(*) FROM t WHERE b='auto'").fetchone()[0], 1)
		other.close()
		auto.close()

	def test_async_runner(self):
		async def run():
			conn = await libsql_compat.aconnect(self.path)
			cursor = await conn.execute("INSERT INTO t (b) VALUES ('async')")
			self.assertEqual(cursor.lastrowid, 1)
			await conn.commit()
			cursor = await conn.execute("SELECT b, ts FROM t WHERE b='async'")
			rows = await cursor.fetchall()
			self.assertEqual(rows[0][0], "async")
			# exception mapping crosses the thread boundary
			try:
				await conn.execute("SELECT * FROM nope")
			except sqlite3.OperationalError:
				pass
			else:
				raise AssertionError("expected OperationalError")
			await conn.close()

		asyncio.run(run())
