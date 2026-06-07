# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Awaitable ORM facade (Phase 15) — ``await frappe.aio.get_doc(...)``.

The sync ORM (``frappe.get_doc`` & co.) is unchanged; custom apps port at
their own pace. Each call here runs the sync original on the worker thread
pool (``sync_to_async(thread_sensitive=False)``) so the event loop never
blocks. The pool thread gets a copy of the caller's contextvars Context, so
``frappe.local`` (site, db connection, session) is the request's own — these
are request-context calls, NOT clean-context ones.

Calls serialize on the same per-Database lock as ``frappe.db.aio``: one
request has one connection and the wire protocol is serial, so
``asyncio.gather`` over ORM calls is safe — the DB work just runs one call
at a time while truly independent awaitables (HTTP, cache, sleep) overlap.
"""

from asgiref.sync import sync_to_async

import frappe


def _awaitable(name):
	"""Awaitable wrapper for the ``frappe.<name>`` sync ORM function."""

	def call(*args, **kwargs):
		fn = getattr(frappe, name)
		db = getattr(frappe.local, "db", None)
		if db is None:
			return fn(*args, **kwargs)
		with db.aio._lock:
			return fn(*args, **kwargs)

	call.__name__ = call.__qualname__ = name
	return sync_to_async(call, thread_sensitive=False)


# read path (Phase 15)
get_doc = _awaitable("get_doc")
get_cached_doc = _awaitable("get_cached_doc")
get_lazy_doc = _awaitable("get_lazy_doc")
get_last_doc = _awaitable("get_last_doc")
get_all = _awaitable("get_all")
get_list = _awaitable("get_list")
get_value = _awaitable("get_value")
get_cached_value = _awaitable("get_cached_value")
get_single_value = _awaitable("get_single_value")
get_meta = _awaitable("get_meta")

# write path (Phase 16)
new_doc = _awaitable("new_doc")
delete_doc = _awaitable("delete_doc")
rename_doc = _awaitable("rename_doc")


class AsyncDocumentFacade:
	"""Awaitable view of a Document: ``await doc.aio.save()``.

	Any method works (``insert``, ``delete``, ``submit``, ``cancel``,
	``run_method``, ``db_set``, ...) — same execution model as the module
	functions above: pool thread, caller's request context, per-Database
	lock. Plain attributes pass through unwrapped.
	"""

	def __init__(self, doc):
		self._doc = doc

	def __getattr__(self, name):
		attr = getattr(self._doc, name)
		if not callable(attr):
			return attr

		def call(*args, **kwargs):
			db = getattr(frappe.local, "db", None)
			if db is None:
				return attr(*args, **kwargs)
			with db.aio._lock:
				return attr(*args, **kwargs)

		call.__name__ = call.__qualname__ = name
		return sync_to_async(call, thread_sensitive=False)
