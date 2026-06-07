# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Frappe-native test client (Phase 20.6) — replaces werkzeug.test.Client.

Drives `frappe.app.handle_request` directly with a frappe.http.Request,
inside a copied contextvars context (the same isolation a pool thread gets
in production), then runs the after-response chain — so each request gets
a fresh frappe.local and CANNOT leak the caller's test context, and
response/cleanup semantics match the real server. Cookie jar persists
across requests like werkzeug's use_cookies=True.
"""

import contextvars
import io
import mimetypes
import secrets
from urllib.parse import urlencode

import orjson

from frappe.http import Headers, Request, Response, parse_cookie_header


class TestResponse:
	"""Response wrapper with the werkzeug TestResponse surface tests use."""

	def __init__(self, response: Response):
		self._response = response
		self.status_code = response.status_code
		self.status = response.status
		self.headers = response.headers
		if response.file_path:
			with open(response.file_path, "rb") as f:
				self.data = f.read()
		else:
			self.data = response.get_data()
		if "Content-Length" not in self.headers:
			self.headers.set("Content-Length", str(len(self.data)))

	@property
	def text(self):
		return self.data.decode(errors="replace")

	@property
	def location(self):
		return self.headers.get("Location")

	@property
	def cache_control(self):
		from frappe.http import RequestCacheControl

		return RequestCacheControl(self.headers.get("Cache-Control"))

	def get_data(self, as_text=False):
		return self.text if as_text else self.data

	@property
	def json(self):
		try:
			return orjson.loads(self.data)
		except orjson.JSONDecodeError:
			return None

	@property
	def is_json(self):
		return "application/json" in (self.headers.get("Content-Type") or "")

	def __repr__(self):
		return f"<TestResponse {self.status}>"


def _encode_multipart(data: dict) -> tuple[bytes, str]:
	"""Encode a dict that contains file tuples ((stream|bytes, filename)) the
	way werkzeug's test client did."""
	boundary = f"frappe-test-{secrets.token_hex(16)}"
	out = io.BytesIO()
	for key, value in data.items():
		out.write(f"--{boundary}\r\n".encode())
		if isinstance(value, tuple):
			stream, filename = value[0], value[1]
			content = stream.read() if hasattr(stream, "read") else stream
			if isinstance(content, str):
				content = content.encode()
			content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
			out.write(
				f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
				f"Content-Type: {content_type}\r\n\r\n".encode()
			)
			out.write(content)
		else:
			out.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
			out.write(str(value).encode())
		out.write(b"\r\n")
	out.write(f"--{boundary}--\r\n".encode())
	return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class Client:
	"""werkzeug.test.Client-shaped driver for the native request pipeline."""

	def __init__(self, application=None, use_cookies=True):
		# `application` accepted for call-shape compatibility; the pipeline
		# is always frappe.app.handle_request
		self.cookie_jar: dict[str, str] = {} if use_cookies else None

	def open(
		self,
		path="/",
		method="GET",
		data=None,
		json=None,
		headers=None,
		query_string=None,
		content_type=None,
		follow_redirects=False,
		**kwargs,
	) -> TestResponse:
		import frappe.app

		hdrs = Headers(headers or {})
		body = None

		# absolute URLs (werkzeug client compat): pull out host + path + query
		if path.startswith(("http://", "https://")):
			from urllib.parse import urlsplit

			parts = urlsplit(path)
			hdrs.setdefault("Host", parts.netloc)
			path = parts.path or "/"
			if parts.query and query_string is None:
				query_string = parts.query

		if json is not None:
			body = orjson.dumps(json)
			hdrs.set("Content-Type", "application/json")
		elif isinstance(data, dict):
			if any(isinstance(v, tuple) or hasattr(v, "read") for v in data.values()):
				files_normalized = {
					k: (v if isinstance(v, tuple) else (v, getattr(v, "name", k))) for k, v in data.items()
				}
				body, multipart_type = _encode_multipart(files_normalized)
				hdrs.set("Content-Type", multipart_type)
			else:
				body = urlencode(data, doseq=True).encode()
				hdrs.set("Content-Type", content_type or "application/x-www-form-urlencoded")
		elif data is not None:
			body = data.encode() if isinstance(data, str) else data
			if content_type:
				hdrs.set("Content-Type", content_type)
		elif content_type:
			hdrs.set("Content-Type", content_type)

		if isinstance(query_string, dict):
			query_string = urlencode(query_string, doseq=True)

		if self.cookie_jar:
			hdrs.set("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookie_jar.items()))

		request = Request.from_values(
			method=method,
			path=path,
			query_string=query_string,  # None lets from_values split "?" out of path
			headers=hdrs,
			data=body,
		)
		if body is not None:
			request.headers.set("Content-Length", str(len(body)))

		response = self._run_isolated(request)

		if self.cookie_jar is not None:
			for set_cookie in response.headers.getlist("Set-Cookie"):
				cookie_part = set_cookie.split(";", 1)[0]
				parsed = parse_cookie_header(cookie_part)
				for key, value in parsed.items():
					if "Expires=Thu, 01 Jan 1970" in set_cookie or value == "":
						self.cookie_jar.pop(key, None)
					else:
						self.cookie_jar[key] = value

		result = TestResponse(response)
		result.request = request  # werkzeug TestResponse parity
		if follow_redirects and result.status_code in (301, 302, 303, 307, 308):
			location = result.headers.get("Location", "/")
			next_method = "GET" if result.status_code == 303 else method
			return self.open(location, method=next_method, follow_redirects=True)
		return result

	def _run_isolated(self, request: Request) -> Response:
		"""Run the pipeline in a COPIED context with a FRESH frappe.local —
		the ContextVar holds a dict instance, and copy_context() shares that
		instance, so init(force=True) would mutate the caller's test context.
		Setting a new dict inside the copy gives the request the same blank
		slate a production pool thread gets; the caller's site/db/session
		are untouched."""

		def _run():
			from frappe.utils.local import _contextvar

			_contextvar.set({})
			try:
				return frappe.app.handle_request(request)
			finally:
				frappe.app.after_response_tasks()

		import frappe.app

		return contextvars.copy_context().run(_run)

	def set_cookie(self, key=None, value="", domain=None, **kwargs):
		self.cookie_jar[key] = value

	def delete_cookie(self, key, **kwargs):
		self.cookie_jar.pop(key, None)

	def get(self, path="/", **kwargs):
		return self.open(path, method="GET", **kwargs)

	def post(self, path="/", **kwargs):
		return self.open(path, method="POST", **kwargs)

	def put(self, path="/", **kwargs):
		return self.open(path, method="PUT", **kwargs)

	def patch(self, path="/", **kwargs):
		return self.open(path, method="PATCH", **kwargs)

	def delete(self, path="/", **kwargs):
		return self.open(path, method="DELETE", **kwargs)

	def head(self, path="/", **kwargs):
		return self.open(path, method="HEAD", **kwargs)

	def options(self, path="/", **kwargs):
		return self.open(path, method="OPTIONS", **kwargs)
