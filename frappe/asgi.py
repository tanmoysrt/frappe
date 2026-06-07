# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Raw ASGI3 entrypoint wrapping the existing Werkzeug WSGI app (Phase 1).

Single-process light mode: boot via `python3 app.py` at bench root, which
runs uvicorn programmatically and sets a sized ThreadPoolExecutor as the
loop's default executor. All sync work runs through
`sync_to_async(thread_sensitive=False)` -> that pool.
(`thread_sensitive=True` serializes every request through one thread —
the 16-rps trap. Never use it.)

No Starlette, no asgiref WsgiToAsgi (serial + never closes the WSGI
iterator). The Werkzeug handler in frappe/app.py is reused unchanged:
Werkzeug parses multipart/content-negotiation/range/conditional straight
from environ.

Env toggles (same as frappe.app.serve): NO_STATICS, USE_PROXY.
"""

import asyncio
import io
import os
import sys
from tempfile import TemporaryFile

from aiofiles.threadpool import wrap as _aio_wrap
from asgiref.sync import sync_to_async

import frappe
import frappe.app
import frappe.dispatch

# big uploads spill to disk past this, so buffering never pins RSS
_SPOOL_MAX = 1024 * 1024

_DONE = object()


def _build_wsgi_app():
	"""Build the same WSGI stack frappe.app.serve() builds, once at import."""
	app = frappe.app.application
	if not os.environ.get("NO_STATICS"):
		# mutates frappe.app.application: SharedData(/assets) + StaticData(/files)
		app = frappe.app.application_with_statics()
	if os.environ.get("USE_PROXY"):
		from werkzeug.middleware.proxy_fix import ProxyFix

		app = ProxyFix(app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
	return app


_wsgi_app = _build_wsgi_app()


async def application(scope, receive, send):
	# socket.io (Phase 13): /socket.io long-polling + websocket, same loop.
	# Lazy import keeps the HTTP path bootable without python-socketio.
	if scope["type"] in ("http", "websocket") and scope["path"].startswith("/socket.io"):
		from frappe import realtime_server

		await realtime_server.application(scope, receive, send)
	elif scope["type"] == "http":
		await _handle_http(scope, receive, send)
	elif scope["type"] == "lifespan":
		await _lifespan(scope, receive, send)
	elif scope["type"] == "websocket":
		# non-socket.io websockets: unsupported
		await receive()
		await send({"type": "websocket.close"})


async def _lifespan(scope, receive, send):
	while True:
		message = await receive()
		if message["type"] == "lifespan.startup":
			# fail-fast guard (Phase 8): a sync frappe.db call on this thread
			# would block the whole process — Database.sql raises instead
			frappe.dispatch.register_loop_thread()
			# SQLite queue workers (Phase 9): asyncio tasks on this loop,
			# idle-cheap when no site uses queue_backend "sqlite"
			from frappe.utils import scheduler, sqlite_queue

			await sqlite_queue.start_workers()
			# in-process scheduler tick (Phase 11); cross-process FileLock +
			# `in_process_scheduler: 0` config flip keep `bench schedule` viable
			scheduler.start_scheduler_task()
			# python socket.io events subscriber (Phase 13)
			from frappe import realtime_server

			realtime_server.start()
			await send({"type": "lifespan.startup.complete"})
		elif message["type"] == "lifespan.shutdown":
			from frappe import realtime_server
			from frappe.utils import scheduler, sqlite_queue

			await realtime_server.stop()
			await scheduler.stop_scheduler_task()
			await sqlite_queue.stop_workers()
			# close per-site DB pools (Phase 6); they live on the bridge
			# loop, so go through a pool thread -> run_coroutine_sync
			await sync_to_async(_shutdown_db_pools, thread_sensitive=False)()
			await send({"type": "lifespan.shutdown.complete"})
			return


def _shutdown_db_pools():
	# lazy: only if the async mariadb backend was ever used
	if mod := sys.modules.get("frappe.database.mariadb.aio"):
		mod.shutdown_pools()


async def _read_body(receive):
	"""Buffer the request body before entering the pool thread (sync Werkzeug
	cannot await receive()). Small bodies stay in memory; past _SPOOL_MAX the
	body spills to a temp file written through aiofiles (Phase 4), so big
	uploads never pin RSS and their disk writes never block the loop.
	Returns None if the client disconnected."""
	buffer = bytearray()
	spill = None  # (raw sync file, aiofiles wrapper) once over _SPOOL_MAX
	while True:
		message = await receive()
		if message["type"] == "http.disconnect":
			if spill:
				await spill[1].close()
			return None
		chunk = message.get("body", b"")
		if spill:
			await spill[1].write(chunk)
		else:
			buffer += chunk
			if len(buffer) > _SPOOL_MAX:
				spill = await _spill_to_disk(buffer)
				buffer = None
		if not message.get("more_body", False):
			break

	if spill:
		raw, wrapped = spill
		await wrapped.flush()
		size = raw.tell()  # fd-offset lookups, no disk wait
		raw.seek(0)
		return raw, size
	return io.BytesIO(buffer), len(buffer)


async def _spill_to_disk(buffer):
	"""Move an over-limit body to disk: open the temp file in a pool thread,
	then write through aiofiles (runs on the loop's default executor — the
	sized pool from app.py). The raw sync file is what Werkzeug reads as
	wsgi.input in its pool thread."""
	raw = await sync_to_async(TemporaryFile, thread_sensitive=False)("w+b")
	wrapped = _aio_wrap(raw, loop=asyncio.get_running_loop(), executor=None)
	await wrapped.write(buffer)
	return raw, wrapped


def _build_environ(scope, body, content_length):
	# ASGI `path` is already %-decoded; Werkzeug wants the raw PATH_INFO,
	# so prefer raw_path to avoid double-decoding e.g. /files names.
	raw_path = scope.get("raw_path")
	path_info = raw_path.split(b"?", 1)[0].decode("latin-1") if raw_path else scope["path"]

	environ = {
		"REQUEST_METHOD": scope["method"],
		"SCRIPT_NAME": scope.get("root_path", ""),
		"PATH_INFO": path_info,
		"QUERY_STRING": scope["query_string"].decode("latin-1"),
		"SERVER_PROTOCOL": "HTTP/" + scope.get("http_version", "1.1"),
		"CONTENT_LENGTH": str(content_length),
		"wsgi.version": (1, 0),
		"wsgi.url_scheme": scope.get("scheme", "http"),
		"wsgi.input": body,
		"wsgi.errors": sys.stderr,
		"wsgi.multithread": True,
		"wsgi.multiprocess": False,
		"wsgi.run_once": False,
	}

	server = scope.get("server") or ("localhost", 80)
	environ["SERVER_NAME"] = server[0]
	environ["SERVER_PORT"] = str(server[1] or 80)
	if client := scope.get("client"):
		environ["REMOTE_ADDR"] = client[0]
		environ["REMOTE_PORT"] = str(client[1])

	for key, value in scope["headers"]:
		name = key.decode("latin-1").upper().replace("-", "_")
		value = value.decode("latin-1")
		if name == "CONTENT_TYPE":
			environ["CONTENT_TYPE"] = value
		elif name == "CONTENT_LENGTH":
			pass  # set above from the actual buffered size
		else:
			name = "HTTP_" + name
			environ[name] = f"{environ[name]},{value}" if name in environ else value

	return environ


def _write_not_supported(data):
	raise NotImplementedError("WSGI write() callable is not supported by frappe.asgi")


def _cleanup(iterable, state):
	"""Per-request cleanup, on EVERY path, exactly once. Known pitfall from
	this bench: asgiref never closed the WSGI iterator -> ClosingIterator's
	close (which runs frappe.destroy, rate limiter, recorder, after_response)
	never ran -> leaked 1 DB conn/request -> `1040 Too many connections`."""
	if state.get("cleaned"):
		return
	state["cleaned"] = True
	try:
		if iterable is not None and (close := getattr(iterable, "close", None)):
			close()
	finally:
		frappe.destroy()  # defensive: idempotent, guards close() raising early


def _next_chunk(iterator, iterable, state):
	try:
		return next(iterator)
	except StopIteration:
		# cleanup HERE, before the terminal ASGI send: uvicorn starts the
		# next request on this connection as soon as the response completes,
		# so cleanup queued after it (in the FIFO pool) lags under load —
		# measured: DB conns piled to 150+ at -c10 -> 1040 Too many
		# connections. Closing before the final send bounds open conns to
		# the number of concurrent clients.
		_cleanup(iterable, state)
		return _DONE


async def _handle_http(scope, receive, send):
	buffered = await _read_body(receive)
	if buffered is None:
		return
	body, content_length = buffered

	environ = _build_environ(scope, body, content_length)
	response = {}

	def start_response(status, headers, exc_info=None):
		if exc_info:
			try:
				if response.get("started"):
					raise exc_info[1].with_traceback(exc_info[2])
			finally:
				exc_info = None
		response["status"] = int(status.split(" ", 1)[0])
		response["headers"] = headers
		return _write_not_supported

	iterable = None
	try:
		# the whole Werkzeug/frappe request runs in one pool thread;
		# start_response is called inside it (werkzeug Response.__call__)
		iterable = await sync_to_async(_wsgi_app, thread_sensitive=False)(environ, start_response)

		await send(
			{
				"type": "http.response.start",
				"status": response["status"],
				"headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response["headers"]],
			}
		)
		response["started"] = True

		# stream the body chunk by chunk — each next() may block (disk reads
		# for direct_passthrough file wrappers), so it runs on the pool.
		# Pulling one chunk at a time gives natural backpressure; the body is
		# never materialized.
		iterator = iter(iterable)
		next_chunk = sync_to_async(_next_chunk, thread_sensitive=False)
		while (chunk := await next_chunk(iterator, iterable, response)) is not _DONE:
			if chunk:
				await send({"type": "http.response.body", "body": chunk, "more_body": True})
		await send({"type": "http.response.body", "body": b"", "more_body": False})
	finally:
		# rare path: error/disconnect before the stream finished cleanly
		if not response.get("cleaned"):
			await sync_to_async(_cleanup, thread_sensitive=False)(iterable, response)
		if isinstance(body, io.BytesIO):
			body.close()
		else:
			# spilled temp file: close unlinks it on disk — keep that off the loop
			await sync_to_async(body.close, thread_sensitive=False)()
