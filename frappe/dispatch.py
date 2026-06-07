# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Async-aware handler dispatch (Phase 2).

Routing knows about async; handlers may be sync or ``async def``. Sync
handlers keep running in the worker thread pool (third-party apps need zero
changes). Async handlers run on the main event loop.

Two entrypoints:

- ``dispatch()`` — loop-side (async): await async handlers natively, push
  sync ones to the pool via ``sync_to_async(thread_sensitive=False)``.
- ``dispatch_sync()`` — pool-thread-side (sync): today's WSGI request path.
  Sync handlers run inline (this thread already *is* a pool worker); async
  handlers bridge to the running loop with ``async_to_sync``.

Fail-fast guard, inherited from asgiref: ``async_to_sync`` raises when
called from the loop thread itself, so an async handler that calls a sync
facade on the loop gets an immediate exception instead of silently blocking
the whole process.
"""

import asyncio
import gc
import inspect
import os
import threading
from functools import partial

from asgiref.sync import async_to_sync, sync_to_async


def is_async_callable(obj) -> bool:
	"""True if ``obj`` is ultimately an ``async def``, looked up through
	``functools.partial``, ``@functools.wraps`` chains (e.g. the
	type-validation wrapper applied by ``@frappe.whitelist()``) and callable
	instances with an async ``__call__``."""
	while True:
		if isinstance(obj, partial):
			obj = obj.func
		elif hasattr(obj, "__wrapped__"):
			obj = inspect.unwrap(obj)
		else:
			break

	if inspect.iscoroutinefunction(obj):
		return True

	# callable instance with an async __call__ (not plain functions/methods,
	# and not classes — type.__call__ is the constructor, never async)
	if inspect.isroutine(obj) or isinstance(obj, type):
		return False
	call = inspect.getattr_static(type(obj), "__call__", None)
	return call is not None and inspect.iscoroutinefunction(inspect.unwrap(call))


async def dispatch(handler, *args, **kwargs):
	"""Loop-side dispatch: await async handlers natively; run sync handlers
	in the thread pool — never on the loop thread."""
	if is_async_callable(handler):
		result = handler(*args, **kwargs)
		# sync @wraps wrappers around async defs (e.g. whitelist's argument
		# type validation) run inline above and hand back the coroutine
		if inspect.isawaitable(result):
			result = await result
		return result

	return await sync_to_async(handler, thread_sensitive=False)(*args, **kwargs)


def dispatch_sync(handler, *args, **kwargs):
	"""Sync-side dispatch for the current (pool-thread) request path.

	Sync handlers run inline — this thread is already a pool worker, no extra
	hop. Async handlers are submitted to the main event loop and this thread
	blocks for the result; asgiref runs the coroutine in this thread's
	contextvars Context, so ``frappe.local`` is the same dict on both sides
	(proven by test_asgi_contextvar_spike). Outside a server (CLI, RQ worker)
	asgiref spins up a one-shot loop instead — same result, still works.
	"""
	if is_async_callable(handler):
		return async_to_sync(dispatch)(handler, *args, **kwargs)
	return handler(*args, **kwargs)


def dispatch_hook(handler, *args, **kwargs):
	"""``dispatch_sync`` for ORM hooks (controller methods, doc_events).

	Sync hooks run inline, untouched. Async hooks bridge to the loop — and
	if the current thread holds the per-Database serialization lock (it does
	when the ORM call came through ``doc.aio`` / ``frappe.aio``), the lock is
	released for the duration: the async hook's ``await frappe.db.aio.*``
	runs on another pool thread and would deadlock on it, while this thread
	is parked here with its connection idle, so releasing is safe.
	"""
	if not is_async_callable(handler):
		return handler(*args, **kwargs)

	import frappe

	db = getattr(frappe.local, "db", None)
	lock = db.aio._lock if db is not None else None
	held = lock is not None and lock.held_by_current_thread()
	if held:
		lock.release()
	try:
		return dispatch_sync(handler, *args, **kwargs)
	finally:
		if held:
			lock.acquire()


# --- sync→async bridge for loop-bound clients (Phase 3+) ---------------------
#
# async_to_sync with no loop around runs each call in a brand-new one-shot
# loop. Async clients (redis.asyncio, later aiomysql/aiosqlite) bind their
# pooled connections to the loop they were created on, so the second call
# from CLI/bench/patches hits a connection from a dead loop ("Event loop is
# closed"). The bridge below is one persistent loop in a daemon thread —
# every sync caller in the process funnels through it, so the client's
# connection pool lives on exactly one loop, forever.

_bridge = {"loop": None, "pid": None}
_bridge_lock = threading.Lock()

# threads that run an event loop (uvicorn's, the bridge's). A sync DB call
# on one of these blocks the WHOLE process, not one worker — Database.sql
# checks this set and raises instead (Phase 8 fail-fast guard). Plain set:
# adds are rare (one per loop thread), reads are a lock-free O(1) lookup.
_loop_thread_ids = set()


def register_loop_thread():
	"""Mark the current thread as an event-loop thread."""
	_loop_thread_ids.add(threading.get_ident())


def on_loop_thread() -> bool:
	"""True when called from a registered event-loop thread."""
	return threading.get_ident() in _loop_thread_ids


def get_bridge_loop() -> asyncio.AbstractEventLoop:
	"""Return the process-wide bridge loop, starting it on first use.

	Fork-safe: threads don't survive fork, so a child process (RQ worker)
	gets a fresh loop instead of submitting to a dead one.
	"""
	if _bridge["loop"] is None or _bridge["pid"] != os.getpid():
		with _bridge_lock:
			if _bridge["loop"] is None or _bridge["pid"] != os.getpid():
				loop = asyncio.new_event_loop()
				threading.Thread(
					target=_run_bridge_loop, args=(loop,), name="frappe-bridge-loop", daemon=True
				).start()
				_bridge["loop"] = loop
				_bridge["pid"] = os.getpid()
	return _bridge["loop"]


def _run_bridge_loop(loop):
	register_loop_thread()
	loop.run_forever()


def _bridge_loop_if_running():
	"""The bridge loop iff it's live for this pid — never starts one."""
	loop = _bridge["loop"]
	if loop is not None and _bridge["pid"] == os.getpid() and not loop.is_closed():
		return loop
	return None


async def _gc_collect():
	return gc.collect()


async def gc_collect_safe():
	"""``gc.collect()`` whose finalizers run on the bridge-loop thread.

	The async DB drivers (aiomysql) keep their connections on the bridge loop;
	a connection's teardown chain (Connection -> StreamWriter -> transport ->
	``loop._remove_reader``/``_remove_writer``) mutates that loop's selector.
	asyncio selectors are NOT thread-safe, so running that finalization from any
	other thread — the server's 30s idle-trim tick, or any thread that happens
	to trip an automatic collection — corrupts the bridge loop's epoll state
	mid-I/O and wedges every DB call funnelling through it (reproduced:
	gc.collect() on the main loop thread hard-hangs all DB I/O). Confine the
	collection (and therefore the finalizers) to the bridge-loop thread.

	Awaitable and non-blocking for the caller's loop: it hands the collection to
	the bridge loop and awaits the result via a wrapped future. Falls back to an
	inline collect when no bridge loop is running (sqlite light mode never
	starts one — aiosqlite uses a worker thread + queue, not a loop selector, so
	it has no cross-thread teardown hazard)."""
	loop = _bridge_loop_if_running()
	if loop is None or loop is asyncio.get_running_loop():
		return gc.collect()
	return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_gc_collect(), loop))


def run_coroutine_sync(coro):
	"""Run ``coro`` on the bridge loop and block for its result.

	The coroutine runs in the caller's contextvars Context (captured by
	``call_soon_threadsafe``), so ``frappe.local`` is the same dict on both
	sides. Fail-fast guard: calling this from a loop thread would block that
	loop — raise instead, same contract as ``async_to_sync``.
	"""
	try:
		asyncio.get_running_loop()
	except RuntimeError:
		return asyncio.run_coroutine_threadsafe(coro, get_bridge_loop()).result()

	coro.close()
	raise RuntimeError("run_coroutine_sync called from an event loop thread - await the async API instead.")
