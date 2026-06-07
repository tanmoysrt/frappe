# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""psycopg3 async backend with per-site connection pools (Phase 23).

Same Database semantics as the psycopg2 backend — only the driver edge
changes: connections come from a per-site ``psycopg_pool.AsyncConnectionPool``
living on the bridge loop, reached through the sync adapters in
frappe.database.aio. Enabled bench-wide with ``use_async_db: 1`` in
common_site_config.json; the psycopg2 sync path is one flag flip away.

psycopg3 (not aiopg) because aiopg runs libpq in async/autocommit mode and
``conn.commit()`` raises — frappe is transaction-per-request and needs real
async commit/rollback, which ``psycopg.AsyncConnection`` provides.

Pool rules mirror the aiomysql backend (see mariadb/aio.py):
- one pool per site (= per credentials/db), created lazily under a lock
- min_size=0, max_size from common config ``db_pool_size`` (default 5)
- ``max_lifetime`` recycles long-lived conns; ``max_idle`` lets the pool
  shrink cold sites back to zero resident connections
- release rolls back first — never return a dirty connection
- bounded acquire wait (``db_pool_acquire_timeout``, default 10 s) → 503

NOTE: this backend is structurally complete and import/parity-checked, but
has NOT been exercised against a live PostgreSQL server in this environment
(none available). Verify against a real PG instance before production use.
"""

import asyncio
import atexit
import logging
import os
import time
from contextlib import contextmanager
from functools import partial

from psycopg import AsyncConnection, IsolationLevel
from psycopg_pool import AsyncConnectionPool

import frappe
from frappe.database.aio import BridgedConnection, BridgedCursor, run_in_clean_context
from frappe.database.postgres.database import PostgresDatabase
from frappe.dispatch import run_coroutine_sync

DEFAULT_POOL_SIZE = 5
DEFAULT_ACQUIRE_TIMEOUT = 10
DEFAULT_IDLE_TIMEOUT = 300
DEFAULT_POOL_RECYCLE = 1800

# process-wide registry, keyed by connection settings. Fork-safe like the
# bridge loop: a child pid gets a fresh registry instead of pools bound to
# the parent's (dead-in-child) bridge loop and worker threads.
_registry = {"pid": None, "pools": {}, "lock": None}

_logger = logging.getLogger("frappe.database.pool")


class PoolAcquireTimeoutError(Exception):
	"""All pool connections busy past the acquire timeout."""

	http_status_code = 503


def _reg():
	if _registry["pid"] != os.getpid():
		_registry.update(pid=os.getpid(), pools={}, lock=asyncio.Lock())
	return _registry


async def _configure(conn: AsyncConnection):
	"""Per-connection setup the pool re-applies on (re)connect: psycopg2's
	backend pinned REPEATABLE READ, so match it. Takes effect next txn."""
	conn.isolation_level = IsolationLevel.REPEATABLE_READ


async def _close_all_pools():
	reg = _reg()
	for key, pool in list(reg["pools"].items()):
		del reg["pools"][key]
		await pool.close()


def shutdown_pools():
	"""Close every pool (lifespan shutdown / CLI exit). Sync + idempotent."""
	if _registry["pid"] == os.getpid() and _registry["pools"]:
		run_coroutine_sync(_close_all_pools())


atexit.register(shutdown_pools)


def _quarantine_inherited_pools():
	"""after-fork (child) hook: drop the parent's pools without closing them
	(close would touch sockets/epoll the child shares with the parent) and
	hand the child a fresh registry. psycopg_pool's worker threads don't
	exist in the child anyway, so the inherited pool object is already dead."""
	if _registry["pid"] == os.getpid() or not _registry["pools"]:
		return
	_registry.update(pid=os.getpid(), pools={}, lock=asyncio.Lock())


os.register_at_fork(after_in_child=_quarantine_inherited_pools)


async def _get_pool(key, conninfo, maxsize, max_lifetime, max_idle):
	"""Lazily create (under the registry lock) or fetch the pool for ``key``.
	Module function fed pre-read config — runs in a clean context (see
	run_in_clean_context), so it must not touch frappe.local."""
	reg = _reg()
	pool = reg["pools"].get(key)
	if pool is None:
		async with reg["lock"]:
			pool = reg["pools"].get(key)
			if pool is None:
				pool = AsyncConnectionPool(
					conninfo,
					min_size=0,
					max_size=maxsize,
					max_lifetime=max_lifetime,
					max_idle=max_idle,
					configure=_configure,
					open=False,
				)
				await pool.open(wait=False)
				reg["pools"][key] = pool
	return pool


async def _acquire(key, conninfo, maxsize, max_lifetime, max_idle, acquire_timeout):
	pool = await _get_pool(key, conninfo, maxsize, max_lifetime, max_idle)
	try:
		conn = await asyncio.wait_for(pool.getconn(), timeout=acquire_timeout)
	except TimeoutError:
		raise PoolAcquireTimeoutError(
			f"No free database connection after {acquire_timeout}s (pool max_size "
			f"{maxsize}) — raise db_pool_size or reduce thread-pool concurrency"
		)
	return conn


async def _release_conn(aconn, key):
	"""Roll back uncommitted state, then hand the connection back to its pool.
	Module-level (not a Database method) so the adapter holds no reference
	back to the Database instance."""
	reg = _reg()
	pool = reg["pools"].get(key)
	if pool is None:
		await aconn.close()
		return
	try:
		await aconn.rollback()
	except Exception:
		await aconn.close()  # broken connection: never return it to the pool
	await pool.putconn(aconn)


class BridgedPostgresConnection(BridgedConnection):
	"""psycopg3's ``AsyncConnection.cursor()`` is a plain (non-coroutine) call
	returning an AsyncCursor whose execute/fetch ARE coroutines — so unlike
	aiomysql, cursor() must not be bridged, only the cursor methods are."""

	def cursor(self, *args, **kwargs):
		return BridgedCursor(self._aconn.cursor(*args, **kwargs))


class AsyncPostgresDatabase(PostgresDatabase):
	"""PostgresDatabase on psycopg3 + per-site pools (driver edge only)."""

	def get_connection(self):
		# config + site context read HERE (caller thread); the acquire
		# coroutine then runs context-free on the bridge loop
		key = self._pool_key()
		coro = _acquire(
			key,
			self._conninfo(),
			maxsize=frappe.get_common_conf("db_pool_size") or DEFAULT_POOL_SIZE,
			max_lifetime=frappe.get_common_conf("db_pool_recycle") or DEFAULT_POOL_RECYCLE,
			max_idle=frappe.get_common_conf("db_pool_idle_timeout") or DEFAULT_IDLE_TIMEOUT,
			acquire_timeout=frappe.get_common_conf("db_pool_acquire_timeout") or DEFAULT_ACQUIRE_TIMEOUT,
		)
		aconn = run_coroutine_sync(run_in_clean_context(coro))
		return BridgedPostgresConnection(aconn, releaser=partial(_release_conn, key=key))

	def _pool_key(self):
		return (self.socket, self.host, self.port, self.user, self.cur_db_name)

	def _conninfo(self) -> str:
		"""libpq connection string (psycopg_pool takes a conninfo, not kwargs)."""
		parts = [f"dbname={self.cur_db_name}", f"user={self.user}"]
		if host := (self.host or self.socket):
			parts.append(f"host={host}")
		if self.password:
			parts.append(f"password={self.password}")
		if not self.socket and self.port:
			parts.append(f"port={self.port}")
		return " ".join(parts)

	@property
	def last_query(self):
		# psycopg3's AsyncCursor has no .query attribute (psycopg2 did); the
		# debug/last-query path falls back to the raw query frappe tracked
		return getattr(self, "_last_executed_query", None)

	@contextmanager
	def unbuffered_cursor(self):
		# psycopg3 server-side (named) cursors stream rows without buffering,
		# same intent as psycopg2's named-cursor path in the sync backend
		try:
			if not self._conn:
				self.connect()
			original_cursor = self._cursor
			new_cursor = self._cursor = self._conn.cursor(name="ss_cursor")
			yield
		finally:
			self._cursor = original_cursor
			new_cursor.close()
