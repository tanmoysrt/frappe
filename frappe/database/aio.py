# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Bridged sync adapters over async DB drivers (Phase 5+).

The whole `Database` class (SQL building, transaction semantics, savepoints,
commit/rollback hooks) talks to PEP 249 connection/cursor objects. Async
backends (aiosqlite, aiomysql) keep that contract: the async driver is
wrapped in sync adapters that submit every I/O call to the process-wide
bridge loop (frappe.dispatch.get_bridge_loop) — all driver coroutines and
pooled connections live on exactly one loop, so a request's transaction
stays on one connection no matter which pool thread runs it.

Async callers (async whitelisted handlers, Phase 8) get the same API
awaitable via ``frappe.db.aio`` — each call runs the sync method on the
worker thread pool, so the event loop never blocks on DB I/O.
"""

from asgiref.sync import sync_to_async

from frappe.dispatch import run_coroutine_sync


async def _close_connection(aconn):
	"""Default releaser: standalone (unpooled) connections just close."""
	await aconn.close()


class BridgedCursor:
	"""Sync PEP 249 cursor facade over an async driver cursor.

	Coroutine methods bridge to the loop; plain attributes (description,
	rowcount, lastrowid, _executed, mogrify) pass through via __getattr__.
	"""

	def __init__(self, acursor):
		self._acursor = acursor

	def execute(self, query, args=None):
		return run_coroutine_sync(self._acursor.execute(query, args))

	def fetchone(self):
		return run_coroutine_sync(self._acursor.fetchone())

	def fetchmany(self, size=1):
		return run_coroutine_sync(self._acursor.fetchmany(size))

	def fetchall(self):
		return run_coroutine_sync(self._acursor.fetchall())

	def close(self):
		return run_coroutine_sync(self._acursor.close())

	def __getattr__(self, name):
		return getattr(self._acursor, name)


class BridgedConnection:
	"""Sync PEP 249 connection facade over an async driver connection.

	``releaser`` is an async callable disposing of the connection on
	close(): plain close for standalone connections (default), rollback +
	return-to-pool for pooled ones (Phase 6) — never just dropped.
	"""

	def __init__(self, aconn, releaser=_close_connection):
		self._aconn = aconn
		self._releaser = releaser

	def cursor(self, *args, **kwargs):
		return BridgedCursor(run_coroutine_sync(self._aconn.cursor(*args, **kwargs)))

	def commit(self):
		return run_coroutine_sync(self._aconn.commit())

	def rollback(self):
		return run_coroutine_sync(self._aconn.rollback())

	def select_db(self, db_name):
		return run_coroutine_sync(self._aconn.select_db(db_name))

	def close(self):
		return run_coroutine_sync(self._releaser(self._aconn))

	def __getattr__(self, name):
		return getattr(self._aconn, name)


class AsyncDatabaseFacade:
	"""Awaitable view of a Database: ``await frappe.db.aio.get_value(...)``.

	Each call runs the sync method on the worker thread pool
	(``sync_to_async(thread_sensitive=False)``), so the event loop never
	blocks; driver I/O still funnels through the bridge loop. Works for
	every backend — async drivers and plain pymysql alike.
	"""

	def __init__(self, db):
		self._db = db
		self._wrappers = {}

	def __getattr__(self, name):
		if wrapper := self._wrappers.get(name):
			return wrapper
		attr = getattr(self._db, name)
		if not callable(attr):
			return attr
		wrapper = sync_to_async(attr, thread_sensitive=False)
		self._wrappers[name] = wrapper
		return wrapper
