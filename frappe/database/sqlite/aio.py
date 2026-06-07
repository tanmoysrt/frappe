# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""aiosqlite backend (Phase 5): proving ground for the async DB stack.

Same SQL/transaction semantics as SQLiteDatabase — only the driver edge
changes: connection and cursor are aiosqlite objects living on the bridge
loop, reached through the sync adapters in frappe.database.aio. Enabled
bench-wide with ``use_async_db: 1`` in common_site_config.json (Phase 19).
"""

import sqlite3

from frappe.database.aio import BridgedConnection, run_in_clean_context
from frappe.database.sqlite.database import SQLiteDatabase
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
		# site context read HERE (caller thread); the connect coroutine runs
		# context-free — aiosqlite's worker thread inherits the ambient
		# context at creation and would otherwise pin this request's
		# frappe.local for the connection's whole life
		db_path = self.get_db_path()
		return BridgedConnection(run_coroutine_sync(run_in_clean_context(_aconnect(db_path, read_only))))


async def _aconnect(db_path, read_only: bool):
	# libsql engine (Phase 22), the only engine for the main DB: same
	# thread-runner shape aiosqlite had; REGEXP is native (no
	# create_function), type conversion lives in the compat adapter
	from frappe.database.sqlite import libsql_compat

	target = f"file:{db_path}?mode=ro" if read_only else str(db_path)
	conn = await libsql_compat.aconnect(target, timeout=15, detect_types=sqlite3.PARSE_DECLTYPES)
	for pragma, value in PRAGMAS.items():
		cursor = await conn.execute(f"PRAGMA {pragma}={value}")
		await cursor.fetchall()  # libsql: drain so commit isn't blocked
	return conn
