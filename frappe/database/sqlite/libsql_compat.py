# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""sqlite3-compatible adapter over the libsql binding (Phase 22).

The PyPI ``libsql`` binding (Rust, ex libsql-experimental) speaks a thin
DB-API subset: plain tuples, bare ``ValueError`` for every engine error, no
row_factory, no type converters, no ``create_function``. This module gives
it the exact ``sqlite3`` surface frappe's sqlite backends use, in ONE place:

- exceptions are re-raised as the REAL ``sqlite3`` exception classes
  (message-pattern mapped), so ``except sqlite3.IntegrityError`` and the
  SQLiteExceptionUtil matchers work unchanged on either engine
- ``Row`` mirrors ``sqlite3.Row`` (index + case-sensitive name access)
- fetch paths apply the same timestamp/date/time conversion that the
  stdlib backend gets from PARSE_DECLTYPES. The binding exposes no column
  decltypes, so conversion is value-shaped (strict full-match ISO
  patterns). Known deviation: a TEXT column holding an exact ISO string
  comes back typed (stdlib would only convert declared columns).
- ``create_function("regexp", ...)`` is a no-op — libsql ships a NATIVE
  REGEXP, which is also why this engine exists here: the python-callback
  regexp of the stdlib backend serializes on the interpreter, the native
  one does not. Other user functions (regexp_replace) cannot be emulated;
  registration warns once and SQL using them fails at the engine.
- ``aconnect``/``AsyncConnection`` is the aiosqlite-shaped thread runner
  (one worker thread owns the connection; every call is a coroutine), so
  ``BridgedConnection`` and the queue workers run unchanged on libsql.

Engine choice: ``sqlite_engine`` in common_site_config.json —
"stdlib" | "libsql".
"""

import asyncio
import queue
import re
import sqlite3
import threading
import warnings
import weakref
from datetime import date, datetime, time

import libsql

# ISO shapes guarding conversion (a typed column should only ever hold
# these; junk passes through as str instead of crashing, unlike stdlib)
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d{1,6})?$")

# decltypes the stdlib backend registers converters for
_CONVERTED_DECLTYPES = ("timestamp", "date", "time")

# per-database-file column->decltype maps (schema is stable across the
# per-request connection churn; rebuilt when an unknown column appears)
_decltype_maps: dict[str, dict[str, str | None]] = {}

# ValueError message -> sqlite3 exception class (libsql raises bare
# ValueError for everything; sqlite error strings are stable across forks)
_INTEGRITY_PATTERNS = (
	"UNIQUE constraint failed",
	"NOT NULL constraint failed",
	"FOREIGN KEY constraint failed",
	"CHECK constraint failed",
	"PRIMARY KEY constraint failed",
)
_PROGRAMMING_PATTERNS = (
	"syntax error",
	"incomplete input",
	"Cannot operate on a closed",
)


def _map_exception(exc: BaseException) -> BaseException:
	message = str(exc)
	if any(p in message for p in _INTEGRITY_PATTERNS):
		return sqlite3.IntegrityError(message)
	if any(p in message for p in _PROGRAMMING_PATTERNS):
		return sqlite3.ProgrammingError(message)
	return sqlite3.OperationalError(message)


def _adapt_params(parameters):
	"""Mirror stdlib sqlite3's default adapters: datetime/date/time bind as
	ISO strings (the binding rejects them outright)."""
	adapted = []
	for value in parameters:
		if isinstance(value, datetime):
			adapted.append(value.isoformat(sep=" "))
		elif isinstance(value, date | time):
			adapted.append(value.isoformat())
		else:
			adapted.append(value)
	return tuple(adapted)


def _convert_by_decltype(value, decltype):
	"""Mirror the stdlib backend's PARSE_DECLTYPES converters, keyed by the
	column's declared type (the binding hides decltypes, so the Connection
	carries a schema-derived column map)."""
	if not isinstance(value, str) or not value or not value[0].isdigit():
		return value
	try:
		if decltype == "timestamp" and _DATETIME_RE.match(value):
			return datetime.fromisoformat(value)
		if decltype == "date" and _DATE_RE.match(value):
			return date.fromisoformat(value)
		if decltype == "time" and _TIME_RE.match(value):
			return time.fromisoformat(value)
	except ValueError:
		pass
	return value


class Row:
	"""sqlite3.Row-compatible: index, slice and column-name access."""

	__slots__ = ("_names", "_values")

	def __init__(self, names, values):
		self._names = names
		self._values = values

	def keys(self):
		return list(self._names)

	def __getitem__(self, key):
		if isinstance(key, str):
			try:
				return self._values[self._names.index(key)]
			except ValueError:
				raise IndexError(f"No item with key {key!r}")
		return self._values[key]

	def __iter__(self):
		return iter(self._values)

	def __len__(self):
		return len(self._values)

	def __eq__(self, other):
		return tuple(self) == tuple(other)

	def __repr__(self):
		return f"<Row {dict(zip(self._names, self._values, strict=False))!r}>"


class Cursor:
	"""sqlite3.Cursor facade over a libsql cursor."""

	def __init__(self, raw, connection: "Connection"):
		self._raw = raw
		self.connection = connection
		self.arraysize = 1
		self._live = False  # statement executed but maybe not fully stepped
		connection._cursors.add(self)

	# -- passthrough metadata ------------------------------------------------

	@property
	def description(self):
		return self._raw.description

	@property
	def lastrowid(self):
		return self._raw.lastrowid

	@property
	def rowcount(self):
		return self._raw.rowcount

	def _names(self):
		description = self._raw.description
		return [column[0] for column in description] if description else []

	# -- execution -----------------------------------------------------------

	def execute(self, sql, parameters=()):
		try:
			self._raw.execute(sql, _adapt_params(parameters) if parameters else ())
		except Exception as e:
			raise _map_exception(e) from e
		self._live = True
		return self

	def executemany(self, sql, seq_of_parameters):
		try:
			self._raw.executemany(sql, [_adapt_params(p) for p in seq_of_parameters])
		except Exception as e:
			raise _map_exception(e) from e
		return self

	def executescript(self, script):
		try:
			self._raw.executescript(script)
		except Exception as e:
			raise _map_exception(e) from e
		return self

	# -- fetching (converters + row_factory applied here) ---------------------

	def _wrap(self, row):
		if row is None:
			return None
		if self.connection.detect_types:
			decltypes = self.connection._column_decltypes(self._names())
			converted = tuple(
				_convert_by_decltype(value, decltype) if decltype else value
				for value, decltype in zip(row, decltypes, strict=False)
			)
		else:
			converted = tuple(row)
		factory = self.connection.row_factory
		if factory is None:
			return converted
		if factory in (Row, sqlite3.Row):
			return Row(self._names(), converted)
		return factory(self, converted)

	def fetchone(self):
		try:
			return self._wrap(self._raw.fetchone())
		except Exception as e:
			raise _map_exception(e) from e

	def fetchmany(self, size=None):
		try:
			rows = self._raw.fetchmany(size if size is not None else self.arraysize)
		except Exception as e:
			raise _map_exception(e) from e
		return [self._wrap(row) for row in rows or ()]

	def fetchall(self):
		try:
			rows = self._raw.fetchall()
		except Exception as e:
			raise _map_exception(e) from e
		self._live = False  # statement fully stepped
		# non-SELECT statements yield None from the binding, not []
		return [self._wrap(row) for row in rows or ()]

	def _drain(self):
		"""Finish stepping an open statement so commit/rollback can run —
		stdlib sqlite3 resets statements implicitly; libsql refuses with
		'SQL statements in progress'. frappe always fetches result sets
		before committing, so this is a no-op in practice."""
		if self._live and self._raw is not None:
			try:
				self._raw.fetchall()
			except Exception:
				pass
			self._live = False

	def __iter__(self):
		while (row := self.fetchone()) is not None:
			yield row

	def close(self):
		# the binding has no cursor.close(); dropping the reference is it
		self._drain()
		self._raw = None

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		self.close()


class Connection:
	"""sqlite3.Connection facade over a libsql connection."""

	def __init__(self, raw, detect_types: int = 0, database: str | None = None):
		self._raw = raw
		self.row_factory = None
		self.detect_types = detect_types
		self._database = database  # decltype-map cache key (shared per file)
		self._decltype_map: dict[str, str | None] | None = None
		self._cursors: weakref.WeakSet = weakref.WeakSet()
		self._lock = threading.RLock()  # libsql conns are not thread-proven

	def _build_decltype_map(self) -> dict[str, str | None]:
		decltype_map: dict[str, str | None] = {}
		cursor = self._raw.cursor()
		cursor.execute(
			"SELECT p.name, lower(p.type) FROM sqlite_master m "
			"JOIN pragma_table_info(m.name) p WHERE m.type='table'"
		)
		for column, decltype in cursor.fetchall() or ():
			decltype = decltype if decltype in _CONVERTED_DECLTYPES else None
			if column in decltype_map and decltype_map[column] != decltype:
				decltype_map[column] = None  # ambiguous across tables
			else:
				decltype_map[column] = decltype
		return decltype_map

	def _column_decltypes(self, names):
		"""Column-name -> declared-type map from the schema. Cached per
		DATABASE FILE (connections churn per request; the schema doesn't),
		refreshed when an unknown column name shows up — covers DDL. A name
		declared with DIFFERENT types across tables maps to None (no
		conversion) rather than guessing."""
		decltype_map = self._decltype_map
		if decltype_map is None and self._database:
			decltype_map = _decltype_maps.get(self._database)
		if decltype_map is None or any(n not in decltype_map for n in names):
			decltype_map = self._build_decltype_map()
			# unknown names stay unknown after a rebuild (aliases, expressions)
			for name in names:
				decltype_map.setdefault(name, None)
			if self._database:
				_decltype_maps[self._database] = decltype_map
		self._decltype_map = decltype_map
		return [decltype_map.get(n) for n in names]

	def cursor(self):
		return Cursor(self._raw.cursor(), self)

	def execute(self, sql, parameters=()):
		cursor = self.cursor()
		cursor.execute(sql, parameters)
		return cursor

	def executemany(self, sql, seq_of_parameters):
		cursor = self.cursor()
		cursor.executemany(sql, seq_of_parameters)
		return cursor

	def executescript(self, script):
		cursor = self.cursor()
		cursor.executescript(script)
		return cursor

	def _drain_cursors(self):
		for cursor in list(self._cursors):
			cursor._drain()

	def commit(self):
		self._drain_cursors()
		try:
			self._raw.commit()
		except Exception as e:
			raise _map_exception(e) from e

	def rollback(self):
		self._drain_cursors()
		try:
			self._raw.rollback()
		except Exception as e:
			raise _map_exception(e) from e

	def close(self):
		try:
			self._raw.close()
		except Exception as e:
			raise _map_exception(e) from e

	@property
	def in_transaction(self):
		return self._raw.in_transaction

	@property
	def isolation_level(self):
		return self._raw.isolation_level

	def create_function(self, name, narg, func, **kwargs):
		"""libsql has no user-defined functions. REGEXP is NATIVE in libsql
		(the reason this engine is interesting), so registering it is a
		no-op; anything else warns once and SQL calling it will fail."""
		if name.lower() == "regexp":
			return
		warnings.warn(
			f"libsql engine cannot register SQL function {name!r} — "
			"queries using it will fail (sqlite_engine: stdlib supports it)",
			stacklevel=2,
		)

	def sync(self):
		"""Embedded-replica sync passthrough (no-op for plain local files)."""
		return self._raw.sync()

	def interrupt(self):  # parity stub; nothing in the hot path uses it
		raise NotImplementedError("libsql binding exposes no interrupt()")


def connect(
	database,
	timeout: float = 5.0,
	detect_types: int = 0,  # sqlite3.PARSE_DECLTYPES enables typed reads
	isolation_level: str | None = "DEFERRED",
	uri: bool = False,
	**kwargs,
) -> Connection:
	"""sqlite3.connect-shaped constructor for the libsql engine.

	`file:...?mode=ro` strings work with or without uri=True (the binding
	auto-detects). busy_timeout is applied as a PRAGMA (no timeout kwarg in
	the binding)."""
	raw = libsql.connect(str(database), isolation_level=isolation_level)
	conn = Connection(raw, detect_types=detect_types, database=str(database))
	if timeout:
		conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
	return conn


# ---------------------------------------------------------------------------
# async runner (aiosqlite-shaped) — option (a) from the spec: one worker
# thread owns the (adapter) connection, every call is a coroutine resolved
# by that thread. BridgedConnection/queue workers use this unchanged.
# ---------------------------------------------------------------------------


class AsyncCursor:
	def __init__(self, conn: "AsyncConnection", cursor: Cursor):
		self._conn = conn
		self._cursor = cursor

	@property
	def description(self):
		return self._cursor.description

	@property
	def lastrowid(self):
		return self._cursor.lastrowid

	@property
	def rowcount(self):
		return self._cursor.rowcount

	async def execute(self, sql, parameters=None):
		await self._conn._run(self._cursor.execute, sql, parameters or ())
		return self

	async def executemany(self, sql, seq_of_parameters):
		await self._conn._run(self._cursor.executemany, sql, seq_of_parameters)
		return self

	async def fetchone(self):
		return await self._conn._run(self._cursor.fetchone)

	async def fetchmany(self, size=None):
		return await self._conn._run(self._cursor.fetchmany, size)

	async def fetchall(self):
		return await self._conn._run(self._cursor.fetchall)

	async def close(self):
		self._cursor.close()


class AsyncConnection:
	"""aiosqlite.Connection-shaped wrapper: a daemon worker thread owns the
	sync adapter connection; coroutines submit callables and await futures.
	One-thread-per-connection keeps the engine's threading rules satisfied
	under both GIL and free-threaded builds."""

	def __init__(self, conn: Connection):
		self._conn = conn
		self._queue: queue.SimpleQueue = queue.SimpleQueue()
		self._thread = threading.Thread(target=self._worker, daemon=True, name="libsql_async")
		self._closed = False
		self._thread.start()

	def _worker(self):
		while True:
			item = self._queue.get()
			if item is None:
				return
			future, loop, fn, args = item
			try:
				result = fn(*args)
			except BaseException as e:
				loop.call_soon_threadsafe(self._reject, future, e)
			else:
				loop.call_soon_threadsafe(self._resolve, future, result)

	@staticmethod
	def _resolve(future, result):
		if not future.cancelled():
			future.set_result(result)

	@staticmethod
	def _reject(future, exc):
		if not future.cancelled():
			future.set_exception(exc)

	async def _run(self, fn, *args):
		if self._closed:
			raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
		loop = asyncio.get_running_loop()
		future = loop.create_future()
		self._queue.put((future, loop, fn, args))
		return await future

	@property
	def row_factory(self):
		return self._conn.row_factory

	@row_factory.setter
	def row_factory(self, value):
		self._conn.row_factory = value

	async def cursor(self):
		return AsyncCursor(self, self._conn.cursor())

	async def execute(self, sql, parameters=None):
		cursor = AsyncCursor(self, self._conn.cursor())
		await cursor.execute(sql, parameters or ())
		return cursor

	async def executescript(self, script):
		return await self._run(self._conn.executescript, script)

	async def execute_fetchall(self, sql, parameters=None):
		"""aiosqlite convenience: execute + fetchall in one worker hop."""

		def _execute_fetchall():
			cursor = self._conn.cursor()
			cursor.execute(sql, parameters or ())
			return cursor.fetchall()

		return await self._run(_execute_fetchall)

	async def commit(self):
		await self._run(self._conn.commit)

	async def rollback(self):
		await self._run(self._conn.rollback)

	async def create_function(self, name, narg, func, **kwargs):
		self._conn.create_function(name, narg, func, **kwargs)

	async def close(self):
		if self._closed:
			return
		await self._run(self._conn.close)
		self._closed = True
		self._queue.put(None)  # stop the worker


async def aconnect(
	database, timeout: float = 5.0, isolation_level="DEFERRED", detect_types: int = 0, **kwargs
) -> AsyncConnection:
	"""Async constructor mirroring ``aiosqlite.connect`` usage here: the
	connection is CREATED on the worker thread too (engine threading rule)."""
	placeholder = AsyncConnection.__new__(AsyncConnection)
	placeholder._queue = queue.SimpleQueue()
	placeholder._closed = False
	placeholder._thread = threading.Thread(
		target=placeholder._worker, daemon=True, name="libsql_async"
	)
	placeholder._thread.start()
	placeholder._conn = await placeholder._run(connect, database, timeout, detect_types, isolation_level)
	return placeholder
