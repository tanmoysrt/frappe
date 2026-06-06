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

import inspect
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
	call = getattr(type(obj), "__call__", None)
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
