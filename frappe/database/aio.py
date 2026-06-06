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

import asyncio
import contextvars

from asgiref.sync import sync_to_async

from frappe.dispatch import run_coroutine_sync


async def run_in_clean_context(coro):
	"""Run ``coro`` in a Task with an empty contextvars.Context.

	Long-lived driver objects capture the ambient context at creation —
	asyncio transports keep it on their reader Handle, aiosqlite's worker
	thread inherits it, background tasks hold it. Created inside a request,
	that pins the request's whole frappe.local dict (db, request, ...) in
	memory for the connection's lifetime. Connection/pool creation therefore
	runs context-free: read config in the caller and pass plain values in;
	frappe.local does not exist inside ``coro``.
	"""
	return await asyncio.get_running_loop().create_task(coro, context=contextvars.Context())


async def _await(awaitable):
	return await awaitable


def _bridge(awaitable):
	"""run_coroutine_sync for any awaitable — aiomysql hands out
	_ContextManager awaitables (cursor(), acquire()) which
	run_coroutine_threadsafe rejects; wrapping makes them real coroutines."""
	return run_coroutine_sync(_await(awaitable))


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
		return _bridge(self._acursor.execute(query, args))

	def fetchone(self):
		return _bridge(self._acursor.fetchone())

	def fetchmany(self, size=1):
		return _bridge(self._acursor.fetchmany(size))

	def fetchall(self):
		return _bridge(self._acursor.fetchall())

	def close(self):
		return _bridge(self._acursor.close())

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
		return BridgedCursor(_bridge(self._aconn.cursor(*args, **kwargs)))

	def commit(self):
		return _bridge(self._aconn.commit())

	def rollback(self):
		return _bridge(self._aconn.rollback())

	def select_db(self, db_name):
		return _bridge(self._aconn.select_db(db_name))

	def close(self):
		# a leak finalizer (if the backend attached one) must not fire after
		# an orderly close — that would release the same conn twice
		if finalizer := self.__dict__.get("_finalizer"):
			finalizer.detach()
		return _bridge(self._releaser(self._aconn))

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
