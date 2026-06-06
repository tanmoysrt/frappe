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

import os
import sys
from tempfile import SpooledTemporaryFile

from asgiref.sync import sync_to_async

import frappe
import frappe.app

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
	if scope["type"] == "http":
		await _handle_http(scope, receive, send)
	elif scope["type"] == "lifespan":
		await _lifespan(scope, receive, send)
	elif scope["type"] == "websocket":
		# no websockets in this process yet — socket.io is still the Node proc
		await receive()
		await send({"type": "websocket.close"})


async def _lifespan(scope, receive, send):
	while True:
		message = await receive()
		if message["type"] == "lifespan.startup":
			# later phases start queue workers / scheduler tick tasks here
			await send({"type": "lifespan.startup.complete"})
		elif message["type"] == "lifespan.shutdown":
			# later phases cancel/await background tasks here
			await send({"type": "lifespan.shutdown.complete"})
			return


async def _read_body(receive):
	"""Buffer the request body before entering the pool thread (sync Werkzeug
	cannot await receive()). Returns None if the client disconnected."""
	body = SpooledTemporaryFile(max_size=_SPOOL_MAX, mode="w+b")
	while True:
		message = await receive()
		if message["type"] == "http.disconnect":
			body.close()
			return None
		body.write(message.get("body", b""))
		if not message.get("more_body", False):
			break
	size = body.tell()
	body.seek(0)
	return body, size


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


def _next_chunk(iterator):
	try:
		return next(iterator)
	except StopIteration:
		return _DONE


def _cleanup(iterable):
	"""Per-request cleanup, on EVERY path. Known pitfall from this bench:
	asgiref never closed the WSGI iterator -> ClosingIterator's close (which
	runs frappe.destroy, rate limiter, recorder, after_response) never ran ->
	leaked 1 DB conn/request -> `1040 Too many connections`."""
	try:
		if iterable is not None and (close := getattr(iterable, "close", None)):
			close()
	finally:
		frappe.destroy()  # defensive: idempotent, guards close() raising early


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
		while (chunk := await next_chunk(iterator)) is not _DONE:
			if chunk:
				await send({"type": "http.response.body", "body": chunk, "more_body": True})
		await send({"type": "http.response.body", "body": b"", "more_body": False})
	finally:
		await sync_to_async(_cleanup, thread_sensitive=False)(iterable)
		body.close()
