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
import mimetypes
import os
import sys
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from tempfile import TemporaryFile

import aiofiles
from aiofiles.threadpool import wrap as _aio_wrap
from asgiref.sync import sync_to_async

import frappe
import frappe.app
import frappe.dispatch

# big uploads spill to disk past this, so buffering never pins RSS
_SPOOL_MAX = 1024 * 1024

_DONE = object()

# native static serving (Phase 20.2)
_STATIC_CHUNK = 256 * 1024
_STATIC_MAX_AGE = 60 * 60 * 12  # werkzeug SharedDataMiddleware default


def _build_wsgi_app():
	"""Build the same WSGI stack frappe.app.serve() builds, once at import."""
	if os.environ.get("USE_PROFILER"):
		from werkzeug.middleware.profiler import ProfilerMiddleware

		# assign the global: application_with_statics() wraps frappe.app.application
		frappe.app.application = ProfilerMiddleware(
			frappe.app.application, sort_by=("cumtime", "calls"), restrictions=(200,)
		)
	app = frappe.app.application
	# statics (/assets + public /files) are served natively by _serve_static
	# (Phase 20.2) — the werkzeug SharedData/StaticData middlewares are no
	# longer wrapped in (application_with_statics remains for rollback)
	if os.environ.get("USE_PROXY"):
		from werkzeug.middleware.proxy_fix import ProxyFix

		app = ProxyFix(app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
	return app


_wsgi_app = _build_wsgi_app()


def _scope_header(scope, name: bytes) -> str | None:
	for key, value in scope["headers"]:
		if key == name:
			return value.decode("latin-1")
	return None


def _static_file_for(scope) -> Path | None:
	"""Map /assets and public /files URLs to disk, with traversal guard.
	Returns None for paths this handler doesn't own (fall through to app)."""
	path = scope["path"]
	sites_path = os.environ.get("SITES_PATH", ".")
	if path.startswith("/assets/"):
		base = Path(sites_path, "assets").resolve()
		rest = path[len("/assets/") :]
	elif path.startswith("/files/"):
		# same resolution as the old StaticDataMiddleware loader:
		# <sites>/<site from bound site or Host>/public/files/<rest>
		from frappe.utils import get_site_name

		site = get_site_name(frappe.app._site or _scope_header(scope, b"host") or "")
		base = Path(sites_path, site, "public", "files").resolve()
		rest = path[len("/files/") :]
	else:
		return None
	if "\x00" in rest:
		return None
	full = (base / rest).resolve()
	if not full.is_relative_to(base):  # ../ traversal
		return None
	return full


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
	"""Single byte-range only (curl -r / video seeking). None = ignore/416."""
	if not header.startswith("bytes=") or "," in header:
		return None
	start_s, _, end_s = header[6:].partition("-")
	try:
		if start_s:
			start = int(start_s)
			end = min(int(end_s), size - 1) if end_s else size - 1
		else:  # suffix range: last N bytes
			start = max(size - int(end_s), 0)
			end = size - 1
	except ValueError:
		return None
	if start > end or start >= size:
		return None
	return start, end


async def _serve_static(scope, send) -> bool:
	"""Serve /assets and public /files straight from the loop: stat + aiofiles
	streaming, ETag/Last-Modified 304s, single-range 206s. Returns False when
	the request isn't a static hit (app handles it). Private files stay on the
	app path (permission checks)."""
	if scope["method"] not in ("GET", "HEAD"):
		return False
	full = _static_file_for(scope)
	if full is None:
		return False
	try:
		stat = await sync_to_async(os.stat, thread_sensitive=False)(full)
	except OSError:
		return False
	if not os.path.isfile(full):  # cheap after stat warmed the dentry cache
		return False

	size = stat.st_size
	mtime = int(stat.st_mtime)
	etag = f'"frappe-{mtime}-{size}"'
	last_modified = formatdate(mtime, usegmt=True)
	headers = [
		(b"content-type", (mimetypes.guess_type(str(full))[0] or "application/octet-stream").encode()),
		(b"accept-ranges", b"bytes"),
		(b"etag", etag.encode()),
		(b"last-modified", last_modified.encode()),
		(b"cache-control", f"public, max-age={_STATIC_MAX_AGE}".encode()),
	]

	# force-download for risky extensions on /files (old patch_start_response)
	if scope["path"].startswith("/files/"):
		from urllib.parse import quote

		from frappe.utils.response import FORCE_DOWNLOAD_EXTENSIONS

		if scope["path"].lower().endswith(FORCE_DOWNLOAD_EXTENSIONS):
			headers.append(
				(b"content-disposition", f"attachment; filename*=UTF-8''{quote(full.name)}".encode())
			)

	# conditional GET -> 304 (ETag wins over If-Modified-Since, like werkzeug)
	if_none_match = _scope_header(scope, b"if-none-match")
	if_modified_since = _scope_header(scope, b"if-modified-since")
	not_modified = False
	if if_none_match:
		not_modified = etag in {tag.strip() for tag in if_none_match.split(",")}
	elif if_modified_since:
		try:
			not_modified = int(parsedate_to_datetime(if_modified_since).timestamp()) >= mtime
		except (TypeError, ValueError):
			pass
	if not_modified:
		await send({"type": "http.response.start", "status": 304, "headers": headers})
		await send({"type": "http.response.body", "body": b""})
		return True

	start, length, status = 0, size, 200
	if range_header := _scope_header(scope, b"range"):
		byte_range = _parse_range(range_header, size)
		if byte_range is None:
			await send(
				{
					"type": "http.response.start",
					"status": 416,
					"headers": [(b"content-range", f"bytes */{size}".encode())],
				}
			)
			await send({"type": "http.response.body", "body": b""})
			return True
		start, end = byte_range
		length, status = end - start + 1, 206
		headers.append((b"content-range", f"bytes {start}-{end}/{size}".encode()))

	headers.append((b"content-length", str(length).encode()))
	await send({"type": "http.response.start", "status": status, "headers": headers})
	if scope["method"] == "HEAD":
		await send({"type": "http.response.body", "body": b""})
		return True

	async with aiofiles.open(full, "rb") as f:
		if start:
			await f.seek(start)
		remaining = length
		while remaining > 0:
			chunk = await f.read(min(_STATIC_CHUNK, remaining))
			if not chunk:
				break
			remaining -= len(chunk)
			await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})
		if remaining > 0:  # file truncated mid-stream; terminate cleanly
			await send({"type": "http.response.body", "body": b"", "more_body": False})
	return True


async def application(scope, receive, send):
	# socket.io (Phase 13): /socket.io long-polling + websocket, same loop.
	# Lazy import keeps the HTTP path bootable without python-socketio.
	if scope["type"] in ("http", "websocket") and scope["path"].startswith("/socket.io"):
		from frappe import realtime_server

		await realtime_server.application(scope, receive, send)
	elif scope["type"] == "http":
		# native /assets + public /files (Phase 20.2): aiofiles streaming,
		# no environ build, no pool hop for the common cache-hit (304) case.
		# Misses fall through to the app (website routes, proper 404 page).
		if not os.environ.get("NO_STATICS") and await _serve_static(scope, send):
			return
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


def serve(port=None, site=None, sites_path=".", proxy=False):
	"""Programmatic uvicorn server — the framework-owned replacement for the
	gunicorn / werkzeug run_simple paths (Phase 20). `bench serve` lands here;
	bench-root app.py adds malloc tuning + re-exec on top of the same core.

	Knobs (common config, env override): asgi_pool_size / FRAPPE_POOL_SIZE,
	asgi_limit_concurrency / FRAPPE_LIMIT_CONCURRENCY, webserver_port /
	FRAPPE_PORT, asgi_thread_stack / FRAPPE_THREAD_STACK.
	"""
	import threading
	from concurrent.futures import ThreadPoolExecutor

	import uvicorn

	import frappe.app

	if site:
		frappe.app._site = site
	frappe.app._sites_path = sites_path
	os.environ["SITES_PATH"] = sites_path
	if proxy:
		os.environ["USE_PROXY"] = "1"

	def knob(key, default, env):
		value = os.environ.get(env)
		if value is None:
			value = frappe.get_common_site_config(sites_path).get(key)
		return int(value) if value is not None else default

	pool_size = knob("asgi_pool_size", 2 * (os.cpu_count() or 1), "FRAPPE_POOL_SIZE")
	limit_concurrency = knob("asgi_limit_concurrency", 4 * pool_size, "FRAPPE_LIMIT_CONCURRENCY")
	port = int(port) if port else knob("webserver_port", 8000, "FRAPPE_PORT")
	thread_stack = knob("asgi_thread_stack", 512 * 1024, "FRAPPE_THREAD_STACK")

	async def _main():
		loop = asyncio.get_running_loop()
		# smaller stacks: default 8 MB per pool thread is pure waste here
		threading.stack_size(thread_stack)
		loop.set_default_executor(
			ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="frappe_sync")
		)
		threading.stack_size(0)
		config = uvicorn.Config(
			"frappe.asgi:application",
			host="0.0.0.0",
			port=port,
			loop="asyncio",
			interface="asgi3",
			lifespan="on",
			limit_concurrency=limit_concurrency,
			log_level=os.environ.get("FRAPPE_LOG_LEVEL", "info"),
			access_log=False,
		)
		await uvicorn.Server(config).serve()

	asyncio.run(_main())


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
