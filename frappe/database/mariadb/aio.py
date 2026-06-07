# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""aiomysql backend with per-site connection pools (Phase 6).

Same Database semantics as the pymysql backend — only the driver edge
changes: connections come from a per-site aiomysql pool living on the
bridge loop, reached through the sync adapters in frappe.database.aio.
Enabled bench-wide with ``use_async_db: 1`` in common_site_config.json
(Phase 19); pymysql fallback is one flag flip away.

Pool rules (spec):
- one pool per site (= per credentials/db), created lazily under an
  asyncio.Lock so two concurrent first-requests can't double-create
- minsize=0, maxsize from common config ``db_pool_size`` (default 5) —
  thread-pool concurrency must not exceed Σ maxsize over hot sites
- bounded acquire wait (``db_pool_acquire_timeout``, default 10 s) —
  a full pool raises 503 instead of deadlocking the request
- release always rolls back first — never return a dirty connection
- ``pool_recycle`` + ping-on-acquire drop conns past MySQL wait_timeout
- idle pools (no conn in use, untouched > ``db_pool_idle_timeout``,
  default 300 s) are closed entirely so cold sites hold zero connections
  and zero buffer memory
"""

import asyncio
import atexit
import logging
import os
import time
import weakref
from contextlib import contextmanager
from functools import partial

import aiomysql

import frappe
from frappe.database.aio import BridgedConnection, run_in_clean_context
from frappe.database.mariadb.database import MariaDBDatabase
from frappe.dispatch import run_coroutine_sync

EVICT_INTERVAL = 60  # seconds between idle sweeps
DEFAULT_POOL_SIZE = 5
DEFAULT_ACQUIRE_TIMEOUT = 10
DEFAULT_IDLE_TIMEOUT = 300
DEFAULT_POOL_RECYCLE = 1800  # < default MySQL wait_timeout (28800)

# process-wide registry, keyed by connection settings. Fork-safe like the
# bridge loop: a child pid gets a fresh registry instead of pools bound to
# the parent's (dead-in-child) bridge loop.
_registry = {"pid": None, "pools": {}, "last_used": {}, "idle_timeout": {}, "lock": None, "evict": None}

# stdlib logger: the evict task runs on the bridge loop with no site context,
# so frappe.logger() (which wants frappe.local) is off limits there
_logger = logging.getLogger("frappe.database.pool")


class PoolAcquireTimeoutError(Exception):
	"""All pool connections busy past the acquire timeout."""

	http_status_code = 503


def _reg():
	if _registry["pid"] != os.getpid():
		_registry.update(
			pid=os.getpid(), pools={}, last_used={}, idle_timeout={}, lock=asyncio.Lock(), evict=None
		)
	return _registry


async def _evict_idle_pools_once():
	"""Close pools nobody touched lately and with no connection in use.
	pool.close() + wait_closed() frees the connections (and their buffers),
	not just idles them — a cold site drops to zero resident pool memory."""
	reg = _reg()
	now = time.monotonic()
	for key, pool in list(reg["pools"].items()):
		# idle timeout snapshotted at pool creation — no site context here
		if now - reg["last_used"].get(key, now) < reg["idle_timeout"].get(key, DEFAULT_IDLE_TIMEOUT):
			continue
		if pool.size != pool.freesize:  # a request still holds a connection
			continue
		del reg["pools"][key]
		reg["last_used"].pop(key, None)
		reg["idle_timeout"].pop(key, None)
		pool.close()
		await pool.wait_closed()


async def _evict_loop():
	while True:
		await asyncio.sleep(EVICT_INTERVAL)
		try:
			await _evict_idle_pools_once()
		except Exception:
			_logger.exception("idle pool eviction failed")


async def _close_all_pools():
	reg = _reg()
	if reg["evict"]:
		reg["evict"].cancel()
		reg["evict"] = None
	for key, pool in list(reg["pools"].items()):
		del reg["pools"][key]
		# terminate, not close: close()+wait_closed() blocks forever on
		# connections still checked out (CLI exit with frappe.db open, hung
		# requests). Force-dropping gives the same semantics as a sync
		# driver's connection dying at process end — server rolls back.
		pool.terminate()
		await pool.wait_closed()
	reg["last_used"].clear()
	reg["idle_timeout"].clear()


def shutdown_pools():
	"""Close every pool (lifespan shutdown / CLI exit). Sync + idempotent."""
	if _registry["pid"] == os.getpid() and _registry["pools"]:
		run_coroutine_sync(_close_all_pools())


# CLI/bench/patches create no lifespan — close pools at interpreter exit
# (the bridge daemon thread is still alive while atexit handlers run)
atexit.register(shutdown_pools)


# Parent driver objects a forked child must never GC: any __del__ in the
# chain (Connection -> StreamWriter -> transport) ends in transport.close()
# -> loop._remove_reader() -> epoll_ctl(DEL) on the epoll instance the child
# SHARES with the parent — silently unsubscribing the parent's reader, which
# then waits forever for a reply sitting unread in the socket buffer
# (reproduced: fork during CLI, child connects, parent hangs on next read).
_fork_quarantine = {"refs": [], "conn_ids": set()}


def _quarantine_inherited_pools():
	"""after-fork (child) hook: park the parent's pools (and their conns)
	in a process-lifetime quarantine so child GC can't touch shared kernel
	state, then hand the child a fresh registry. The quarantined sockets
	die with the child's fd table — a few objects held in a short-lived
	worker, not a leak that grows."""
	if _registry["pid"] == os.getpid() or not _registry["pools"]:
		return
	_fork_quarantine["refs"].append(_registry["pools"])
	for pool in _registry["pools"].values():
		for conn in [*getattr(pool, "_free", ()), *getattr(pool, "_used", ())]:
			_fork_quarantine["conn_ids"].add(id(conn))
	_registry.update(
		pid=os.getpid(), pools={}, last_used={}, idle_timeout={}, lock=asyncio.Lock(), evict=None
	)


os.register_at_fork(after_in_child=_quarantine_inherited_pools)


async def _get_pool(key, conn_settings, maxsize, pool_recycle, idle_timeout):
	"""Lazily create (under the registry lock) or fetch the pool for ``key``.
	Module function fed pre-read config — runs in a clean context (see
	run_in_clean_context), so it must not touch frappe.local."""
	reg = _reg()
	pool = reg["pools"].get(key)
	if pool is None:
		async with reg["lock"]:
			pool = reg["pools"].get(key)
			if pool is None:
				pool = await aiomysql.create_pool(
					minsize=0,
					maxsize=maxsize,
					pool_recycle=pool_recycle,
					autocommit=False,
					**conn_settings,
				)
				reg["pools"][key] = pool
				reg["idle_timeout"][key] = idle_timeout
				if reg["evict"] is None:
					reg["evict"] = asyncio.ensure_future(_evict_loop())
	reg["last_used"][key] = time.monotonic()
	return pool


async def _acquire(key, conn_settings, maxsize, pool_recycle, idle_timeout, acquire_timeout):
	pool = await _get_pool(key, conn_settings, maxsize, pool_recycle, idle_timeout)
	try:
		conn = await asyncio.wait_for(pool.acquire(), timeout=acquire_timeout)
	except TimeoutError:
		raise PoolAcquireTimeoutError(
			f"No free database connection after {acquire_timeout}s (pool maxsize "
			f"{pool.maxsize}) — raise db_pool_size or reduce thread-pool concurrency"
		)
	try:
		# drop stale conns past wait_timeout/pool_recycle; then re-apply
		# session state pooling can't guarantee (collation has no aiomysql
		# connect kwarg). max_statement_time is re-applied by
		# Database.connect() right after this, per acquire.
		await conn.ping()
		cursor = await conn.cursor()
		await cursor.execute("SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci")
		await cursor.close()
	except BaseException:
		pool.release(conn)
		raise
	return conn


async def _release_conn(aconn, key):
	"""Roll back uncommitted state, then hand the connection back to its pool.
	Module-level (not a Database method) so the adapter holds no reference
	back to the Database instance — that cycle would defeat the prompt
	refcount collection the leak finalizer below relies on."""
	if id(aconn) in _fork_quarantine["conn_ids"]:
		# parent's connection seen from a forked child: hands off entirely —
		# a rollback would WRITE on the parent's socket, a close would
		# epoll_ctl the shared epoll. It dies with the child's fd table.
		return
	reg = _reg()
	pool = reg["pools"].get(key)
	try:
		await aconn.rollback()
	except Exception:
		aconn.close()  # broken connection: never return it to the pool
	if pool is None:
		aconn.close()
	else:
		pool.release(aconn)
		reg["last_used"][key] = time.monotonic()


def _release_leaked(aconn, key):
	"""weakref.finalize callback: a caller dropped the connection without
	close() (e.g. a WSGI iterator that never got closed). A sync driver's
	conn would be closed by refcount GC — pooled conns sit in pool._used
	forever instead, starving the pool. Restore the GC semantics."""
	try:
		if id(aconn) in _fork_quarantine["conn_ids"] or aconn.closed:
			return
		if _registry["pid"] != os.getpid():
			return
		from frappe.dispatch import get_bridge_loop

		asyncio.run_coroutine_threadsafe(_release_conn(aconn, key), get_bridge_loop())
	except Exception:  # interpreter shutdown etc. — conn dies with process
		pass


class AsyncMariaDBDatabase(MariaDBDatabase):
	"""MariaDBDatabase on aiomysql + per-site pools (driver edge only)."""

	def get_connection(self):
		# config + site context read HERE (caller thread); the acquire
		# coroutine then runs context-free on the bridge loop
		key = self._pool_key()
		coro = _acquire(
			key,
			self._aio_connection_settings(),
			maxsize=frappe.get_common_conf("db_pool_size") or DEFAULT_POOL_SIZE,
			pool_recycle=frappe.get_common_conf("db_pool_recycle") or DEFAULT_POOL_RECYCLE,
			idle_timeout=frappe.get_common_conf("db_pool_idle_timeout") or DEFAULT_IDLE_TIMEOUT,
			acquire_timeout=frappe.get_common_conf("db_pool_acquire_timeout") or DEFAULT_ACQUIRE_TIMEOUT,
		)
		aconn = run_coroutine_sync(run_in_clean_context(coro))
		bridged = BridgedConnection(aconn, releaser=partial(_release_conn, key=key))
		finalizer = weakref.finalize(bridged, _release_leaked, aconn, key)
		# GC-only safety net: finalize defaults to ALSO firing at interpreter
		# exit even while the adapter is still alive and owned — that would
		# release the conn under atexit cleanup (console!) and double-release
		finalizer.atexit = False
		bridged._finalizer = finalizer
		return bridged

	def _pool_key(self):
		return (self.socket, self.host, self.port, self.user, self.cur_db_name)

	def _aio_connection_settings(self) -> dict:
		# pymysql settings, translated: aiomysql.connect takes `db` (not
		# `database`), no `collation` kwarg (set per acquire instead), no SSL
		# dict (needs an SSLContext — out of scope for the experiment)
		settings = self.get_connection_settings()
		if "database" in settings:
			settings["db"] = settings.pop("database")
		settings.pop("collation", None)
		if settings.pop("ssl", None):
			raise NotImplementedError("db_ssl_* not supported on the aiomysql backend yet")
		return settings

	def log_query(self, query, query_type, values, debug):
		mogrified_query = self._cursor._acursor._executed
		self.last_query = mogrified_query
		self._log_query(mogrified_query, query_type, debug, query)
		return mogrified_query

	def _clean_up(self):
		# PERF: same intent as the pymysql backend — drop result refs early,
		# but on the wrapped aiomysql cursor/connection
		cursor = self._cursor._acursor
		cursor._rows = None
		cursor._result = None
		cursor._connection._result = None

	@contextmanager
	def unbuffered_cursor(self):
		from aiomysql.cursors import SSCursor

		try:
			if not self._conn:
				self.connect()

			original_cursor = self._cursor
			new_cursor = self._cursor = self._conn.cursor(SSCursor)
			yield
		finally:
			self._cursor = original_cursor
			new_cursor.close()
