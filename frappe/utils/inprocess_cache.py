# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""In-process cache backend (Phase 21) — `cache_backend: "memory"`.

One process (light mode) needs no cache server: `frappe.cache` becomes this
object — plain dicts + one RLock, implementing exactly the RedisWrapper
surface the codebase uses (audited). Values are pickled on write and
unpickled on read, SAME as the redis wrapper, so the backend swap is
invisible (including the mutation-isolation that the network round-trip
used to provide).

Cross-process invalidation (bench CLI -> running server) rides the
``sites/.cache-generation`` bump-file: writers bump it after clear-cache /
migrate; this object stats it at most once per second and flushes when it
changes. Multi-process deployments must keep ``cache_backend: "redis"``.

The lock is a real threading.RLock — load-bearing under free-threaded
Python (Phase 23), cheap under the GIL.
"""

import fnmatch
import os
import pickle
import re
import threading
import time
from collections import namedtuple

import frappe
from frappe.utils import cstr

DEFAULT_PICKLE_PROTOCOL = 5
DEFAULT_MAX_ENTRIES = 50_000  # FIFO-evicted; ~guard against unbounded RSS
_SWEEP_EVERY = 2048  # mutations between amortized expiry sweeps
_GENERATION_FILE = ".cache-generation"
_GENERATION_CHECK_INTERVAL = 1.0  # seconds between bump-file stats

CacheStatistics = namedtuple(
	"CacheStatistics", ["hits", "misses", "capacity", "used", "utilization", "hit_ratio", "healthy"]
)


def _norm(key) -> bytes:
	return key if isinstance(key, bytes) else str(key).encode()


def _dump(value) -> bytes:
	return pickle.dumps(value, protocol=DEFAULT_PICKLE_PROTOCOL)


class _Pipeline:
	"""Buffer of operations executed atomically under the store lock
	(the subset of redis pipelines frappe uses: set/setex/incrby/hdel)."""

	def __init__(self, cache: "InProcessCache"):
		self._cache = cache
		self._ops = []

	def __getattr__(self, name):
		def queue(*args, **kwargs):
			self._ops.append((name, args, kwargs))
			return self

		return queue

	def execute(self):
		results = []
		with self._cache._lock:
			for name, args, kwargs in self._ops:
				results.append(getattr(self._cache, name)(*args, **kwargs))
		self._ops = []
		return results


class InProcessCache:
	"""frappe.cache for the memory backend. Thread-safe; one per process."""

	def __init__(self, max_entries: int | None = None):
		self._lock = threading.RLock()
		self._strings: dict[bytes, bytes] = {}
		self._hashes: dict[bytes, dict[bytes, bytes]] = {}
		self._sets: dict[bytes, set] = {}
		self._lists: dict[bytes, list] = {}
		self._expiries: dict[bytes, float] = {}
		self._list_waiter = threading.Condition(self._lock)
		self._mutations = 0
		self._max_entries = max_entries or int(
			os.environ.get("FRAPPE_CACHE_MAX_ENTRIES") or DEFAULT_MAX_ENTRIES
		)
		self.hits = self.misses = 0
		self._generation = None
		self._generation_checked = 0.0

	# ------------------------------------------------------------------
	# plumbing
	# ------------------------------------------------------------------

	@property
	def aio(self):
		"""Awaitable facade: in-process ops complete inline — no pool hop,
		no bridge, the coroutine just returns the value."""
		return _AsyncFacade(self)

	def __call__(self):
		"""Backward compatibility for frappe.cache()(...) style."""
		return self

	def make_key(self, key, user=None, shared=False):
		from frappe.utils.redis_wrapper import _make_key

		return _make_key(key, user, shared)

	def connected(self):
		return True

	def ping(self):
		return True

	def client_id(self):
		return os.getpid()

	def pipeline(self):
		return _Pipeline(self)

	def execute_command(self, *args, **kwargs):
		if args and str(args[0]).upper() == "INFO":
			used = sum(len(v) for v in self._strings.values())
			return {"used_memory_human": f"{used / (1024 * 1024):.1f}M (in-process)"}
		raise NotImplementedError(
			f"execute_command({args!r}) is not supported by the in-process cache backend; "
			'set cache_backend: "redis" if you need raw redis commands'
		)

	def ft(self, index_name="idx"):
		raise NotImplementedError(
			'redisearch requires cache_backend: "redis" — the in-process backend has no full-text engine'
		)

	# ------------------------------------------------------------------
	# expiry + eviction + cross-process generation
	# ------------------------------------------------------------------

	def _now(self):
		return time.monotonic()

	def _alive(self, key: bytes) -> bool:
		expiry = self._expiries.get(key)
		if expiry is not None and self._now() >= expiry:
			self._drop(key)
			return False
		return True

	def _drop(self, key: bytes):
		self._strings.pop(key, None)
		self._hashes.pop(key, None)
		self._sets.pop(key, None)
		self._lists.pop(key, None)
		self._expiries.pop(key, None)

	def _on_mutation(self):
		self._mutations += 1
		if self._mutations % _SWEEP_EVERY == 0:
			self.sweep()
		if len(self._strings) > self._max_entries:
			with self._lock:
				overflow = len(self._strings) - self._max_entries
				for key in list(self._strings)[:overflow]:  # FIFO: oldest first
					self._drop(key)

	def sweep(self):
		"""Drop expired entries (amortized from mutations; callable from a
		periodic lifespan task too)."""
		now = self._now()
		with self._lock:
			for key in [k for k, expiry in self._expiries.items() if now >= expiry]:
				self._drop(key)

	def check_generation(self):
		"""Cross-process invalidation: flush everything when the bump-file
		changed (bench clear-cache / migrate ran in another process). Stat
		amortized to at most once per second."""
		now = time.monotonic()
		if now - self._generation_checked < _GENERATION_CHECK_INTERVAL:
			return
		self._generation_checked = now
		try:
			stamp = os.stat(self._generation_path()).st_mtime_ns
		except OSError:
			return
		if self._generation is None:
			self._generation = stamp
		elif stamp != self._generation:
			self._generation = stamp
			self.flushall()

	@staticmethod
	def _generation_path():
		sites_path = getattr(frappe.local, "sites_path", None) or "."
		return os.path.join(sites_path, _GENERATION_FILE)

	@staticmethod
	def bump_generation():
		"""Signal running processes to flush (atomic rename, restart-safe)."""
		path = InProcessCache._generation_path()
		tmp = f"{path}.tmp{os.getpid()}"
		with open(tmp, "w") as f:
			f.write(str(time.time()))
		os.replace(tmp, path)

	# ------------------------------------------------------------------
	# low-level commands (redis.Redis subset, already-prefixed keys)
	# ------------------------------------------------------------------

	def get(self, key):
		key = _norm(key)
		self.check_generation()
		with self._lock:
			if not self._alive(key):
				self.misses += 1
				return None
			value = self._strings.get(key)
		if value is None:
			self.misses += 1
		else:
			self.hits += 1
		return value

	def set(self, name=None, value=None, ex=None, **kwargs):
		name = _norm(name if name is not None else kwargs.get("name"))
		raw = value if isinstance(value, bytes) else str(value).encode()
		with self._lock:
			self._strings[name] = raw
			if ex is not None:
				self._expiries[name] = self._now() + ex
			else:
				self._expiries.pop(name, None)
			self._on_mutation()
		return True

	def setex(self, name, time_sec, value):
		return self.set(name, value, ex=time_sec)

	def expire(self, name, time_sec):
		name = _norm(name)
		with self._lock:
			if self._exists_raw(name):
				self._expiries[name] = self._now() + time_sec
				return 1
		return 0

	def ttl(self, name):
		name = _norm(name)
		with self._lock:
			if not self._exists_raw(name):
				return -2
			expiry = self._expiries.get(name)
		return -1 if expiry is None else max(0, int(expiry - self._now()))

	def _exists_raw(self, key: bytes) -> bool:
		return self._alive(key) and (
			key in self._strings or key in self._hashes or key in self._sets or key in self._lists
		)

	def unlink(self, *keys):
		with self._lock:
			count = 0
			for key in keys:
				key = _norm(key)
				if self._exists_raw(key):
					count += 1
				self._drop(key)
		return count

	delete = unlink

	def keys(self, pattern):
		pattern = _norm(pattern)
		self.check_generation()
		with self._lock:
			self.sweep_if_needed()
			alive = [k for k in (*self._strings, *self._hashes, *self._sets, *self._lists) if self._alive(k)]
		return [k for k in alive if fnmatch.fnmatchcase(k.decode("latin-1"), pattern.decode("latin-1"))]

	def sweep_if_needed(self):
		if self._expiries:
			self.sweep()

	def incrby(self, name, amount=1):
		name = _norm(name)
		with self._lock:
			current = int(self._strings.get(name) or 0) if self._alive(name) else 0
			current += amount
			self._strings[name] = str(current).encode()
			self._on_mutation()
			return current

	incr = incrby

	def decrby(self, name, amount=1):
		return self.incrby(name, -amount)

	def flushall(self, *args, **kwargs):
		with self._lock:
			self._strings.clear()
			self._hashes.clear()
			self._sets.clear()
			self._lists.clear()
			self._expiries.clear()
		return True

	flushdb = flushall

	# ------------------------------------------------------------------
	# high-level helpers (RedisWrapper-compatible, frappe-keyed)
	# ------------------------------------------------------------------

	def set_value(self, key, val, user=None, expires_in_sec=None, shared=False):
		key = self.make_key(key, user, shared)
		frappe.local.cache[key] = val
		self.set(name=key, value=_dump(val), ex=expires_in_sec)

	def get_value(self, key, generator=None, user=None, expires=False, shared=False, *, use_local_cache=True):
		original_key = key
		key = self.make_key(key, user, shared)

		local_cache = frappe.local.cache
		if key in local_cache and use_local_cache:
			return local_cache[key]

		val = self.get(key)
		if val is not None:
			val = pickle.loads(val)

		if not expires:
			if val is None and generator:
				val = generator()
				self.set_value(original_key, val, user=user, shared=shared)
			else:
				local_cache[key] = val
		return val

	def expire_key(self, key, time_sec, *, user=None, shared=False):
		return self.expire(self.make_key(key, user, shared), time_sec)

	def get_all(self, key):
		return {key: self.get_value(k) for k in self.get_keys(key)}

	def get_keys(self, key, user=None, shared=False):
		"""Return keys starting with `key`."""
		return self.keys(self.make_key(key + "*", user=user, shared=shared))

	def delete_keys(self, key, user=None, shared=False):
		self.delete_value(self.get_keys(key, user=user, shared=shared), make_keys=False)

	def delete_key(self, *args, **kwargs):
		self.delete_value(*args, **kwargs)

	def delete_value(self, keys, user=None, make_keys=True, shared=False):
		if not keys:
			return
		if not isinstance(keys, list | tuple):
			keys = (keys,)
		if make_keys:
			keys = [self.make_key(k, shared=shared, user=user) for k in keys]
		local_cache = frappe.local.cache
		for key in keys:
			local_cache.pop(key, None)
		self.unlink(*keys)

	# --- lists -----------------------------------------------------------

	def _list(self, key: bytes) -> list:
		if not self._alive(key):
			pass  # dropped
		return self._lists.setdefault(key, [])

	def lpush(self, key, value, user=None, shared=False):
		key = self.make_key(key, user=user, shared=shared)
		with self._list_waiter:
			self._list(_norm(key)).insert(0, _norm(value))
			self._on_mutation()
			self._list_waiter.notify_all()

	def rpush(self, key, value):
		key = self.make_key(key)
		with self._list_waiter:
			self._list(_norm(key)).append(_norm(value))
			self._on_mutation()
			self._list_waiter.notify_all()

	def lpop(self, key, user=None, shared=False):
		key = _norm(self.make_key(key, user=user, shared=shared))
		with self._lock:
			values = self._lists.get(key) if self._alive(key) else None
			return values.pop(0) if values else None

	def rpop(self, key):
		key = _norm(self.make_key(key))
		with self._lock:
			values = self._lists.get(key) if self._alive(key) else None
			return values.pop() if values else None

	def blpop(self, key, timeout=0, user=None, shared=False):
		made = _norm(self.make_key(key, user=user, shared=shared))
		deadline = None if not timeout else time.monotonic() + timeout
		with self._list_waiter:
			while True:
				values = self._lists.get(made) if self._alive(made) else None
				if values:
					return (made, values.pop(0))
				remaining = None if deadline is None else deadline - time.monotonic()
				if remaining is not None and remaining <= 0:
					return None
				self._list_waiter.wait(timeout=remaining if remaining is not None else 1.0)

	def llen(self, key):
		key = _norm(self.make_key(key))
		with self._lock:
			return len(self._lists.get(key, ())) if self._alive(key) else 0

	def lrange(self, key, start, stop):
		key = _norm(self.make_key(key))
		with self._lock:
			values = list(self._lists.get(key, ())) if self._alive(key) else []
		# redis stop is inclusive; -1 means end
		stop = len(values) if stop == -1 else stop + 1
		return values[start:stop]

	def lindex(self, key, index):
		key = _norm(self.make_key(key))
		with self._lock:
			values = self._lists.get(key) if self._alive(key) else None
			try:
				return values[index] if values else None
			except IndexError:
				return None

	def ltrim(self, key, start, stop):
		key = _norm(self.make_key(key))
		with self._lock:
			values = self._lists.get(key) if self._alive(key) else None
			if values is not None:
				end = len(values) if stop == -1 else stop + 1
				self._lists[key] = values[start:end]
		return True

	# --- hashes ----------------------------------------------------------

	def hset(self, name, key, value, shared=False, *args, **kwargs):
		if key is None:
			return
		_name = self.make_key(name, shared=shared)
		frappe.local.cache.setdefault(_name, {})[key] = value
		with self._lock:
			self._hashes.setdefault(_norm(_name), {})[_norm(key)] = _dump(value)
			self._on_mutation()

	def hexists(self, name, key, shared=False):
		if key is None:
			return False
		_name = _norm(self.make_key(name, shared=shared))
		with self._lock:
			return self._alive(_name) and _norm(key) in self._hashes.get(_name, ())

	def exists(self, *names, user=None, shared=None):
		count = 0
		with self._lock:
			for name in names:
				if self._exists_raw(_norm(self.make_key(name, user=user, shared=shared))):
					count += 1
		return count

	def hgetall(self, name):
		_name = _norm(self.make_key(name))
		with self._lock:
			raw = dict(self._hashes.get(_name, ())) if self._alive(_name) else {}
		return {key: pickle.loads(value) for key, value in raw.items()}

	def hget(self, name, key, generator=None, shared=False):
		_name = self.make_key(name, shared=shared)
		local_cache = frappe.local.cache
		if _name not in local_cache:
			local_cache[_name] = {}
		if not key:
			return None
		if key in local_cache[_name]:
			return local_cache[_name][key]

		with self._lock:
			raw_name = _norm(_name)
			value = self._hashes.get(raw_name, {}).get(_norm(key)) if self._alive(raw_name) else None
		if value is not None:
			value = pickle.loads(value)
			local_cache[_name][key] = value
		elif generator:
			value = generator()
			self.hset(name, key, value, shared=shared)
		return value

	def hdel(self, name, keys, shared=False, pipeline=None):
		# pipeline arg kept for API compatibility; everything is atomic here
		_name = self.make_key(name, shared=shared)
		if not isinstance(keys, list | tuple):
			keys = (keys,)
		name_in_local_cache = _name in frappe.local.cache
		with self._lock:
			bucket = self._hashes.get(_norm(_name))
			for key in keys:
				if name_in_local_cache:
					frappe.local.cache[_name].pop(key, None)
				if bucket is not None:
					bucket.pop(_norm(key), None)

	def hdel_names(self, names, key):
		for name in names:
			self.hdel(name, key)

	def hdel_keys(self, name_starts_with, key):
		for name in self.get_keys(name_starts_with):
			name = name.decode() if isinstance(name, bytes) else name
			self.hdel(name.split("|", 1)[1], key)

	def hkeys(self, name):
		_name = _norm(self.make_key(name))
		with self._lock:
			if not self._alive(_name):
				return []
			return list(self._hashes.get(_name, ()))

	# --- sets ------------------------------------------------------------

	def sadd(self, name, *values):
		key = _norm(self.make_key(name))
		with self._lock:
			self._sets.setdefault(key, set()).update(_norm(v) for v in values)
			self._on_mutation()

	def srem(self, name, *values):
		key = _norm(self.make_key(name))
		with self._lock:
			if bucket := self._sets.get(key):
				bucket.difference_update(_norm(v) for v in values)

	def sismember(self, name, value):
		key = _norm(self.make_key(name))
		with self._lock:
			return self._alive(key) and _norm(value) in self._sets.get(key, ())

	def spop(self, name):
		key = _norm(self.make_key(name))
		with self._lock:
			bucket = self._sets.get(key) if self._alive(key) else None
			return bucket.pop() if bucket else None

	def srandmember(self, name, count=None):
		key = _norm(self.make_key(name))
		with self._lock:
			bucket = self._sets.get(key) if self._alive(key) else None
			return next(iter(bucket)) if bucket else None

	def smembers(self, name):
		key = _norm(self.make_key(name))
		with self._lock:
			return set(self._sets.get(key, ())) if self._alive(key) else set()

	# --- stats -------------------------------------------------------------

	@property
	def statistics(self) -> CacheStatistics:
		used = len(self._strings) + len(self._hashes) + len(self._sets) + len(self._lists)
		return CacheStatistics(
			hits=self.hits,
			misses=self.misses,
			capacity=self._max_entries,
			used=used,
			utilization=round(used / self._max_entries, 4),
			hit_ratio=round(self.hits / (self.hits + self.misses), 2) if self.hits else None,
			healthy=True,
		)


class _AsyncFacade:
	"""`frappe.cache.aio` for the memory backend: same object, awaitable
	methods. In-process ops are non-blocking — faster than redis ever was."""

	__slots__ = ("_sync",)

	def __init__(self, sync: InProcessCache):
		self._sync = sync

	def __call__(self):
		return self

	def __getattr__(self, name):
		attr = getattr(self._sync, name)
		if not callable(attr):
			return attr

		async def call(*args, **kwargs):
			return attr(*args, **kwargs)

		call.__name__ = name
		return call


class InProcessClientCache:
	"""frappe.client_cache for the memory backend: the dict IS already
	in-process, so this is a thin alias over InProcessCache — objects are
	stored once (pickled) and memoized unpickled per process with the same
	FIFO+TTL bounds the redis ClientCache used (maxsize 1024, ttl 10 min)."""

	CachedValue = namedtuple("CachedValue", ["value", "expiry"])

	def __init__(self, cache: InProcessCache, maxsize: int = 1024, ttl=10 * 60):
		self.backend = cache
		self.maxsize = maxsize
		self.local_ttl = ttl
		self.lock = threading.RLock()
		self.cache: dict[bytes, InProcessClientCache.CachedValue] = {}
		self.healthy = True
		self.hits = self.misses = 0

	def get_value(self, key, *, shared=False, generator=None):
		key_bytes = _norm(self.backend.make_key(key, shared=shared))
		entry = self.cache.get(key_bytes)
		if entry is not None and time.monotonic() < entry.expiry:
			self.hits += 1
			return entry.value
		self.misses += 1
		# read through the backend (shared=True: key is already prefixed)
		val = self.backend.get_value(key_bytes, shared=True, generator=generator, use_local_cache=False)
		if val is not None:
			self._store(key_bytes, val)
		return val

	def set_value(self, key, val, *, shared=False):
		key_bytes = _norm(self.backend.make_key(key, shared=shared))
		self.backend.set_value(key_bytes, val, shared=True)
		self._store(key_bytes, val)

	def get_doc(self, doctype: str, name: str | None = None):
		if not name:
			name = doctype  # singles
		key = frappe.get_document_cache_key(doctype, name)
		return self.get_value(key, generator=lambda: frappe.get_doc(doctype, name))

	def _store(self, key_bytes: bytes, val):
		if len(self.cache) >= self.maxsize:
			with self.lock:
				self.cache.pop(next(iter(self.cache), None), None)
		with self.lock:
			self.cache[key_bytes] = self.CachedValue(value=val, expiry=time.monotonic() + self.local_ttl)

	def delete_value(self, key, *, shared=False):
		key_bytes = _norm(self.backend.make_key(key, shared=shared))
		self.backend.delete_value(key_bytes, shared=True)
		with self.lock:
			self.cache.pop(key_bytes, None)

	def delete_keys(self, pattern):
		keys = self.backend.get_keys(pattern)
		self.backend.delete_value(keys, shared=True, make_keys=False)
		with self.lock:
			for key in keys:
				self.cache.pop(_norm(key), None)

	def erase_persistent_caches(self, *, doctype=None):
		"""One process: clear worker-local persistent caches directly (the
		redis pubsub signal is meaningless in-process)."""
		import frappe.utils.caching
		from frappe.cache_manager import clear_controller_cache

		clear_controller_cache(doctype, site=getattr(frappe.local, "site", None))
		if not doctype:
			frappe.utils.caching._SITE_CACHE.clear()

	def clear_cache(self):
		with self.lock:
			self.cache.clear()

	@property
	def statistics(self) -> CacheStatistics:
		return CacheStatistics(
			hits=self.hits,
			misses=self.misses,
			capacity=self.maxsize,
			used=len(self.cache),
			healthy=self.healthy,
			utilization=round(len(self.cache) / self.maxsize, 2),
			hit_ratio=round(self.hits / (self.hits + self.misses), 2) if self.hits else None,
		)

	def reset_statistics(self):
		self.hits = self.misses = 0
		self.backend.hits = self.backend.misses = 0
