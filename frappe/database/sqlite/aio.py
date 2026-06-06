# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""aiosqlite backend (Phase 5): proving ground for the async DB stack.

Same SQL/transaction semantics as SQLiteDatabase — only the driver edge
changes: connection and cursor are aiosqlite objects living on the bridge
loop, reached through the sync adapters in frappe.database.aio. Enabled
per site with ``use_async_db: 1`` in site_config.json.
"""

import sqlite3
from datetime import date, datetime, time

import aiosqlite

from frappe.database.aio import BridgedConnection
from frappe.database.sqlite.database import SQLiteDatabase, regexp, regexp_replace
from frappe.dispatch import run_coroutine_sync

# Memory hygiene (spec): sqlite defaults multiply silently across per-site
# connections. Pin the page cache (negative = KiB) and keep mmap off so RSS
# stays predictable; rest matches the sync backend.
PRAGMAS = {
	"journal_mode": "WAL",
	"synchronous": "NORMAL",
	"busy_timeout": 5000,  # milliseconds
	"cache_size": -2048,  # 2 MiB page cache, explicit
	"mmap_size": 0,  # no mmap — deliberate, not inherited
}


class AsyncSQLiteDatabase(SQLiteDatabase):
	"""SQLiteDatabase on aiosqlite. One connection per site (no pool), WAL."""

	def get_connection(self, read_only: bool = False):
		return BridgedConnection(run_coroutine_sync(self._aconnect(read_only)))

	async def _aconnect(self, read_only: bool):
		# converters are registered on the sqlite3 module (global, idempotent)
		sqlite3.register_converter("timestamp", lambda x: datetime.fromisoformat(x.decode()))
		sqlite3.register_converter("date", lambda x: date.fromisoformat(x.decode()))
		sqlite3.register_converter("time", lambda x: time.fromisoformat(x.decode()))

		db_path = self.get_db_path()
		if read_only:
			conn = await aiosqlite.connect(
				f"file:{db_path}?mode=ro",
				uri=True,
				detect_types=sqlite3.PARSE_DECLTYPES,
				timeout=15,
			)
		else:
			conn = await aiosqlite.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=15)

		await conn.create_function("regexp", 2, regexp)
		await conn.create_function("regexp_replace", 3, regexp_replace)
		for pragma, value in PRAGMAS.items():
			await conn.execute(f"PRAGMA {pragma}={value}")
		return conn
