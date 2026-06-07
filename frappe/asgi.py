# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Native ASGI3 entrypoint (Phase 1 wrapper; Phase 20 made it the primary
request path — no WSGI environ, no Werkzeug anywhere).

Single-process light mode: boot via `python3 app.py` at bench root, which
runs uvicorn programmatically and sets a sized ThreadPoolExecutor as the
loop's default executor. Each request: body buffered/spooled on the loop,
a frappe-native Request built from the scope, `frappe.app.handle_request`
run on the pool via `sync_to_async(thread_sensitive=False)`
(`thread_sensitive=True` serializes every request through one thread —
the 16-rps trap. Never use it.), then the native Response sent straight
from the loop (file responses stream via aiofiles with Range/304 support).

Env toggles: NO_STATICS, USE_PROXY.
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
from frappe.http import Request

# big uploads spill to disk past this, so buffering never pins RSS
_SPOOL_MAX = 1024 * 1024

# native static serving (Phase 20.2)
_STATIC_CHUNK = 256 * 1024
_STATIC_MAX_AGE = 60 * 60 * 12  # werkzeug SharedDataMiddleware default


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
			# Phase 24.1: warm ONLY the backend drivers this config uses, so
			# they are import-locked here (boot) not on the first hot request;
			# unconfigured backends stay cold. (24.7 freezes these after warmup.)
			from frappe.utils.preload import preload_configured_backends

			preload_configured_backends()
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
			# Phase 24.7: the import graph + the drivers just preloaded are
			# permanent — move them out of GC scanning so gen2 collections stop
			# walking (and dirtying the pages of) the framework's object graph.
			# A second freeze after warmup (app.py _idle_trim) captures meta /
			# controllers populated by the first requests. Safe under no-GIL.
			import gc

			# preload_configured_backends may have opened pooled connections on
			# the bridge loop; run the collect there (gc_collect_safe) so their
			# finalizers never touch the loop selector cross-thread.
			await frappe.dispatch.gc_collect_safe()
			gc.freeze()
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


def _apply_proxy_headers(scope):
	"""USE_PROXY: trust one hop of X-Forwarded-* (the ~15-line scope rewrite
	that replaces werkzeug's ProxyFix middleware)."""
	headers = dict(scope["headers"])
	new_scope = dict(scope)
	if forwarded_for := headers.get(b"x-forwarded-for"):
		client_ip = forwarded_for.decode("latin-1").split(",")[0].strip()
		new_scope["client"] = (client_ip, 0)
	if forwarded_proto := headers.get(b"x-forwarded-proto"):
		new_scope["scheme"] = forwarded_proto.decode("latin-1").split(",")[0].strip()
	if forwarded_host := headers.get(b"x-forwarded-host"):
		host = forwarded_host.decode("latin-1").split(",")[0].strip().encode("latin-1")
		new_scope["headers"] = [(k, host if k == b"host" else v) for k, v in scope["headers"]]
	return new_scope


def _finish_request(state):
	"""Per-request epilogue, on EVERY path, exactly once, BEFORE the terminal
	ASGI send. Known pitfall from this bench: cleanup queued after the final
	send lags behind new requests in the FIFO pool under load — measured: DB
	conns piled to 150+ at -c10 -> `1040 Too many connections`."""
	if state.get("cleaned"):
		return
	state["cleaned"] = True
	frappe.app.after_response_tasks()


def serve(port=None, site=None, sites_path=".", proxy=False):
	"""Programmatic uvicorn server — the framework-owned light-mode runtime and
	the `bench serve` target (Phase 20; Phase 25 folded the bench-root app.py
	memory tuning — sized pool, idle malloc_trim, post-warmup gc.freeze — in
	here). Bench-root app.py is now only the frappe-free malloc/GIL re-exec
	shim that calls this; the re-exec has to stay out of the frappe package
	because the allocator env must be set before any `import frappe`.

	Knobs — sites/common_site_config.json only (no env vars): asgi_pool_size,
	asgi_limit_concurrency, webserver_port, asgi_thread_stack,
	malloc_trim_interval, loop_debug, log_level.
	"""
	import ctypes
	import gc
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

	conf = frappe.get_common_site_config(sites_path)

	def knob(key, default):
		value = conf.get(key)
		return int(value) if value is not None else default

	cpu = os.cpu_count() or 1
	is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
	gil_disabled = is_gil_enabled is not None and not is_gil_enabled()
	# GIL build: 2 x CPU overlaps I/O waits. Free-threaded (Phase 23): threads
	# run truly parallel, so ~CPU-sized pool avoids CPU oversubscription.
	default_pool = cpu if gil_disabled else 2 * cpu
	# Phase 24.10 lean default: the smallest backend set (sqlite main DB +
	# in-process cache + sqlite queue) has no external I/O to overlap, so a
	# CPU-sized pool is just wasted thread stacks — cap it small.
	lean = (
		conf.get("db_type") == "sqlite"
		and conf.get("cache_backend") in (None, "", "memory")
		and conf.get("queue_backend") in (None, "", "sqlite")
	)
	if lean:
		default_pool = min(default_pool, 4)

	pool_size = knob("asgi_pool_size", default_pool)
	# In-flight cap on CPU, not pool: no-GIL shrinks the pool but serves far
	# more rps, so a pool-derived cap would shed valid load as 503s under burst.
	limit_concurrency = knob("asgi_limit_concurrency", 8 * cpu)
	port = int(port) if port else knob("webserver_port", 8001)
	thread_stack = knob("asgi_thread_stack", 512 * 1024)
	trim_interval = knob("malloc_trim_interval", 30)

	async def _idle_trim():
		"""Replaces gunicorn max_requests recycling: release freed pages back to
		the OS so heap fragmentation doesn't accumulate in a process that runs
		for weeks. malloc_trim(0) is a glibc API, a no-op under tcmalloc."""
		try:
			libc = ctypes.CDLL("libc.so.6")
		except OSError:
			libc = None
		warmed_up = False
		while True:
			await asyncio.sleep(trim_interval)
			# gc.collect() finalizers must run on the bridge-loop thread: a bare
			# gc.collect() here (this is the main uvicorn loop thread) would tear
			# down aiomysql transports cross-thread and wedge all DB I/O.
			await frappe.dispatch.gc_collect_safe()
			# Phase 24.7: one-shot freeze after the first warmup cycle — by now
			# the initial requests have populated meta/controllers; move that
			# now-stable graph out of GC scanning too (lifespan startup already
			# froze the import graph + preloaded drivers). Cumulative, no-GIL safe.
			if not warmed_up:
				gc.freeze()
				warmed_up = True
			if libc is not None:
				try:
					libc.malloc_trim(0)
				except Exception:
					pass

	async def _main():
		loop = asyncio.get_running_loop()
		# smaller stacks: default 8 MB per pool thread is pure waste here
		threading.stack_size(thread_stack)
		loop.set_default_executor(
			ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="frappe_sync")
		)
		threading.stack_size(0)
		gil_status = "unknown" if is_gil_enabled is None else ("disabled" if gil_disabled else "enabled")
		print(
			f"frappe ASGI boot: pool={pool_size}, limit_concurrency={limit_concurrency}, "
			f"port={port}, gil={gil_status}",
			file=sys.stderr,
		)
		if conf.get("loop_debug"):
			# dev: with one loop a blocking call stalls the whole process — log
			# any callback/step that holds it too long
			loop.set_debug(True)
			loop.slow_callback_duration = 0.1
		trim_task = asyncio.ensure_future(_idle_trim())
		config = uvicorn.Config(
			"frappe.asgi:application",
			host="0.0.0.0",
			port=port,
			loop="asyncio",
			interface="asgi3",
			lifespan="on",
			limit_concurrency=limit_concurrency,
			log_level=conf.get("log_level", "info"),
			access_log=False,
		)
		try:
			await uvicorn.Server(config).serve()
		finally:
			trim_task.cancel()

	asyncio.run(_main())


async def _handle_http(scope, receive, send):
	buffered = await _read_body(receive)
	if buffered is None:
		return
	body, content_length = buffered

	if os.environ.get("USE_PROXY"):
		scope = _apply_proxy_headers(scope)

	request = Request.from_scope(scope, body)
	if content_length and not request.headers.get("Content-Length"):
		# chunked transfer: report the actual buffered size
		request.headers.set("Content-Length", str(content_length))

	state = {}
	try:
		# the whole frappe request runs in one pool thread; the contextvars
		# copy gives it an isolated frappe.local (frappe.init force=True)
		response = await sync_to_async(frappe.app.handle_request, thread_sensitive=False)(request)
		await _send_response(scope, send, response, state)
	finally:
		if not state.get("cleaned"):
			await sync_to_async(_finish_request, thread_sensitive=False)(state)
		if isinstance(body, io.BytesIO):
			body.close()
		else:
			# spilled temp file: close unlinks it on disk — keep that off the loop
			await sync_to_async(body.close, thread_sensitive=False)()


async def _send_response(scope, send, response, state):
	"""Send a frappe.http.Response: in-memory bodies directly, file responses
	streamed via aiofiles with Range/conditional support. After-response
	tasks run (on the pool) BEFORE the terminal send — see _finish_request."""
	if response.file_path:
		await _send_file_response(scope, send, response, state)
		return

	headers = response.headers
	body = response.get_data()  # drains iterables; bodies here are in-memory
	if "Content-Length" not in headers:
		headers.set("Content-Length", str(len(body)))
	await send(
		{
			"type": "http.response.start",
			"status": response.status_code,
			"headers": headers.to_asgi_list(),
		}
	)
	if scope["method"] != "HEAD" and body:
		await send({"type": "http.response.body", "body": body, "more_body": True})
	await sync_to_async(_finish_request, thread_sensitive=False)(state)
	await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _send_file_response(scope, send, response, state):
	"""send_file() responses: stat + conditional (ETag/Last-Modified) + single
	Range, streamed with aiofiles — same machinery as native statics."""
	from frappe.http import Response

	full = response.file_path
	try:
		stat = await sync_to_async(os.stat, thread_sensitive=False)(full)
	except OSError:
		fallback = Response("Not Found", status=404, mimetype="text/plain")
		await _send_response(scope, send, fallback, state)
		return

	size = stat.st_size
	mtime = int(stat.st_mtime)
	etag = f'"frappe-{mtime}-{size}"'
	headers = response.headers
	headers.set("Accept-Ranges", "bytes")
	if response.conditional:
		headers.set("ETag", etag)
		headers.set("Last-Modified", formatdate(mtime, usegmt=True))

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
			await send({"type": "http.response.start", "status": 304, "headers": headers.to_asgi_list()})
			await sync_to_async(_finish_request, thread_sensitive=False)(state)
			await send({"type": "http.response.body", "body": b""})
			return

	start, length, status = 0, size, response.status_code
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
			await sync_to_async(_finish_request, thread_sensitive=False)(state)
			await send({"type": "http.response.body", "body": b""})
			return
		range_start, range_end = byte_range
		start, length, status = range_start, range_end - range_start + 1, 206
		headers.set("Content-Range", f"bytes {range_start}-{range_end}/{size}")

	headers.set("Content-Length", str(length))
	await send({"type": "http.response.start", "status": status, "headers": headers.to_asgi_list()})
	if scope["method"] == "HEAD":
		await sync_to_async(_finish_request, thread_sensitive=False)(state)
		await send({"type": "http.response.body", "body": b""})
		return

	async with aiofiles.open(full, "rb") as f:
		if start:
			await f.seek(start)
		remaining = length
		while remaining > 0:
			chunk = await f.read(min(_STATIC_CHUNK, remaining))
			if not chunk:
				break
			remaining -= len(chunk)
			await send({"type": "http.response.body", "body": chunk, "more_body": True})
	await sync_to_async(_finish_request, thread_sensitive=False)(state)
	await send({"type": "http.response.body", "body": b"", "more_body": False})
