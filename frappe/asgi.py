# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""ASGI entrypoint for Frappe.

Run from the sites directory:
    uvicorn frappe.asgi:application --host 0.0.0.0 --port 8001 --workers 8 --loop asyncio

Rendering is CPU-bound and GIL-limited, so worker processes (not threads)
are what scale throughput — same as gunicorn -w.

Env toggles (same as frappe.app.serve): NO_STATICS, USE_PROXY.
"""

import sysconfig
import asyncio

asyncio.iscoroutinefunction()



sysconfig.get_config_var("Py_GIL_DISABLED")

# gevent must patch before anything else imports socket/ssl/time.
# thread=False: uvicorn's threads (and GeventExecutor's hub thread) must stay
#   real threads; patched ones would become greenlets starving the asyncio loop.
# select=False: asyncio keeps the real epoll primitives.
# queue=False: GeventExecutor's task queue must stay a real queue.SimpleQueue.
from gevent import monkey

monkey.patch_all(thread=False, select=False, queue=False)

# Two repairs so uvicorn's multiprocess supervisor survives the patch:
# 1. It pickles the listening socket to spawned workers, and gevent sockets
#    refuse to pickle. Register the stdlib reducer (dup the fd) for the
#    patched class; workers rebuild a plain socket, which is what their
#    asyncio loop needs anyway.
# 2. Its worker ping/pong Pipe() is a socketpair; created via the patched
#    module the fds come out non-blocking and the worker's Connection.recv()
#    dies with BlockingIOError, making the supervisor cycle healthy workers.
#    Hand multiprocessing the original (blocking) socket module.
import multiprocessing.connection
import socket
import types
from multiprocessing import reduction

reduction.ForkingPickler.register(socket.socket, reduction._reduce_socket)

_real_socket = types.ModuleType("socket_unpatched")
_real_socket.__dict__.update(socket.__dict__)
_names = ("socket", "socketpair", "fromfd")
_real_socket.__dict__.update(zip(_names, monkey.get_original("socket", _names), strict=True))
multiprocessing.connection.socket = _real_socket

import functools
import os
import queue
import threading
from concurrent.futures import Executor, Future

import gevent
from asgiref.sync import sync_to_async
from asgiref.wsgi import WsgiToAsgi, WsgiToAsgiInstance

import frappe.app


class GeventExecutor(Executor):
	"""Runs every submitted call as a greenlet on one dedicated gevent thread.

	A regular thread pool breaks under gevent: frappe shares redis/DB clients
	across requests, and a gevent socket created on one thread cannot be used
	from another ("greenlet.error: Cannot switch to a different thread").
	Pinning all greenlets to a single hub thread keeps every gevent object on
	the same hub while requests still interleave on IO. Requires cooperative
	drivers (use_mysqlclient=0 -> pymysql); mysqlclient's C calls would block
	the whole hub.
	"""

	def __init__(self):
		self._tasks = queue.SimpleQueue()  # stays unpatched (queue=False)
		ready = threading.Event()
		threading.Thread(target=self._run, args=(ready,), name="frappe_gevent", daemon=True).start()
		ready.wait()

	def _run(self, ready):
		hub = gevent.get_hub()
		# async_ watcher: the only gevent primitive that may be poked from
		# another thread; wakes the hub to drain the task queue
		self._watcher = hub.loop.async_()
		self._watcher.start(self._drain)
		ready.set()
		hub.join()

	def _drain(self):
		while True:
			try:
				task = self._tasks.get_nowait()
			except queue.Empty:
				return
			gevent.spawn(self._invoke, *task)

	@staticmethod
	def _invoke(future, fn, args, kwargs):
		if not future.set_running_or_notify_cancel():
			return
		try:
			future.set_result(fn(*args, **kwargs))
		except BaseException as e:
			future.set_exception(e)

	def submit(self, fn, /, *args, **kwargs):
		future = Future()
		self._tasks.put((future, fn, args, kwargs))
		self._watcher.send()
		return future


_executor = GeventExecutor()


# WsgiToAsgi runs the WSGI app via sync_to_async(thread_sensitive=True),
# which serializes all requests through one shared thread. Rewrap it to run
# greenlet-per-request on the gevent executor. (__dict__ access fetches the
# raw SyncToAsync wrapper; attribute access would return a bound async partial.)
class _ConcurrentInstance(WsgiToAsgiInstance):
	run_wsgi_app = sync_to_async(
		WsgiToAsgiInstance.__dict__["run_wsgi_app"].func,
		thread_sensitive=False,
		executor=_executor,
	)


class ConcurrentWsgiToAsgi(WsgiToAsgi):
	async def __call__(self, scope, receive, send):
		await _ConcurrentInstance(self.wsgi_application, self.duplicate_header_limit)(scope, receive, send)


def _closing(app):
	"""asgiref iterates the WSGI response but never calls .close() on it
	(PEP 3333 requires it). Frappe runs its per-request cleanup — DB
	disconnect, rate limiter, after_response hooks — from ClosingIterator's
	close, so without this every request leaks a DB connection."""

	@functools.wraps(app)
	def wrapper(environ, start_response):
		iterable = app(environ, start_response)
		try:
			yield from iterable
		finally:
			if close := getattr(iterable, "close", None):
				close()

	return wrapper


def _build_wsgi_app():
	app = frappe.app.application

	if not os.environ.get("NO_STATICS"):
		app = frappe.app.application_with_statics()

	if os.environ.get("USE_PROXY"):
		from werkzeug.middleware.proxy_fix import ProxyFix

		app = ProxyFix(app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

	return _closing(app)


application = ConcurrentWsgiToAsgi(_build_wsgi_app())
