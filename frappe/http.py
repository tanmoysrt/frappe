# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Frappe-native HTTP primitives (Phase 20): Request, Response, Headers,
MultiDict, FileStorage, HTTP errors, redirect/send_file helpers and a small
URL router — the attribute-compatible replacement for the werkzeug surface
frappe actually used. Built directly on the ASGI scope; no WSGI environ.

Only what the codebase uses is implemented (audit-driven). Anything exotic
werkzeug offered (per-key cache-control objects, full content negotiation)
is intentionally absent.
"""

import io
import mimetypes
import os
import re
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import parse_qsl, quote, unquote

import orjson

# -------------------------------------------------------------------------
# datastructures
# -------------------------------------------------------------------------


class Headers:
	"""Case-insensitive, multi-value header collection (werkzeug.Headers
	compatible subset). Iterating yields (key, value) pairs."""

	def __init__(self, defaults=None):
		self._list: list[tuple[str, str]] = []
		if defaults:
			self.update(defaults)

	def get(self, key, default=None, type=None):
		lower = key.lower()
		for k, v in self._list:
			if k.lower() == lower:
				if type is not None:
					try:
						return type(v)
					except ValueError:
						return default
				return v
		return default

	def getlist(self, key):
		lower = key.lower()
		return [v for k, v in self._list if k.lower() == lower]

	def set(self, key, value, **kw):
		self.remove(key)
		self.add(key, value, **kw)

	def add(self, key, value, **kw):
		if kw:
			value = _dump_options_header(value, kw)
		self._list.append((key, str(value)))

	def setdefault(self, key, value):
		if key not in self:
			self.add(key, value)
		return self.get(key)

	def remove(self, key):
		lower = key.lower()
		self._list = [(k, v) for k, v in self._list if k.lower() != lower]

	def pop(self, key, default=None):
		value = self.get(key, default)
		self.remove(key)
		return value

	def update(self, other):
		items = other.items() if hasattr(other, "items") else other
		for key, value in items:
			self.set(key, value)

	def items(self):
		return list(self._list)

	def keys(self):
		return [k for k, _ in self._list]

	def values(self):
		return [v for _, v in self._list]

	def clear(self):
		self._list = []

	def __getitem__(self, key):
		value = self.get(key)
		if value is None:
			raise KeyError(key)
		return value

	def __setitem__(self, key, value):
		self.set(key, value)

	def __delitem__(self, key):
		self.remove(key)

	def __contains__(self, key):
		return self.get(key) is not None

	def __iter__(self):
		return iter(self._list)

	def __len__(self):
		return len(self._list)

	def __bool__(self):
		return bool(self._list)

	def __eq__(self, other):
		return isinstance(other, Headers) and self._list == other._list

	def __repr__(self):
		return f"Headers({self._list!r})"

	def to_asgi_list(self) -> list[tuple[bytes, bytes]]:
		return [(k.encode("latin-1"), v.encode("latin-1")) for k, v in self._list]


def _dump_options_header(value, options: dict) -> str:
	"""'attachment', {'filename': 'x.csv'} -> 'attachment; filename="x.csv"'
	(non-ascii filenames get the RFC 5987 filename* form, like werkzeug)."""
	parts = [str(value)]
	for key, opt in options.items():
		if opt is None:
			continue
		opt = str(opt)
		parts.append(f'{key}="{opt}"' if _OPTION_NEEDS_QUOTES.search(opt) else f"{key}={opt}")
	return "; ".join(parts)


_OPTION_NEEDS_QUOTES = re.compile(r'[\s";,=]|[^\x20-\x7e]|^$')


class MultiDict(dict):
	"""dict storing multiple values per key; plain access returns the first
	value (werkzeug.MultiDict compatible subset)."""

	def __init__(self, mapping=None):
		super().__init__()
		if mapping:
			items = mapping.items() if hasattr(mapping, "items") else mapping
			for key, value in items:
				self.add(key, value)

	def add(self, key, value):
		super().setdefault(key, []).append(value)

	def __iter__(self):
		# overriding __iter__ forces dict.update(self) off CPython's fast
		# path onto keys()+__getitem__ -> consumers see FIRST values, not the
		# internal lists (same trick werkzeug relies on)
		return super().__iter__()

	def __getitem__(self, key):
		return super().__getitem__(key)[0]

	def get(self, key, default=None, type=None):
		try:
			value = self[key]
		except KeyError:
			return default
		if type is not None:
			try:
				return type(value)
			except ValueError:
				return default
		return value

	def getlist(self, key):
		return list(super().get(key, []))

	def __setitem__(self, key, value):
		super().__setitem__(key, [value])

	def setdefault(self, key, default=None):
		if key not in self:
			self[key] = default
		return self[key]

	def items(self, multi=False):
		for key, values in super().items():
			if multi:
				yield from ((key, v) for v in values)
			else:
				yield key, values[0]

	def values(self):
		return [values[0] for values in super().values()]

	def to_dict(self, flat=True):
		if flat:
			return dict(self.items())
		return {key: list(values) for key, values in super().items()}

	def pop(self, key, *args):
		value = super().pop(key, *args)
		return value[0] if isinstance(value, list) and value else value

	def copy(self):
		copied = MultiDict()
		for key, values in super().items():
			for value in values:
				copied.add(key, value)
		return copied


class FileStorage:
	"""Uploaded file (werkzeug.FileStorage compatible subset)."""

	def __init__(self, stream=None, filename=None, name=None, content_type=None, headers=None):
		self.stream = stream or io.BytesIO()
		self.filename = filename
		self.name = name  # form field name
		self.headers = headers or Headers()
		if content_type:
			self.headers.set("Content-Type", content_type)

	@property
	def content_type(self):
		return self.headers.get("Content-Type")

	@property
	def mimetype(self):
		content_type = self.content_type or ""
		return content_type.split(";", 1)[0].strip().lower()

	def read(self, *args):
		return self.stream.read(*args)

	def seek(self, *args):
		return self.stream.seek(*args)

	def save(self, dst, buffer_size=64 * 1024):
		close = False
		if isinstance(dst, str | os.PathLike):
			dst = open(dst, "wb")
			close = True
		try:
			while chunk := self.stream.read(buffer_size):
				dst.write(chunk)
		finally:
			if close:
				dst.close()

	def close(self):
		try:
			self.stream.close()
		except Exception:
			pass

	def __bool__(self):
		return bool(self.filename)

	def __repr__(self):
		return f"<FileStorage: {self.filename!r} ({self.content_type!r})>"


# -------------------------------------------------------------------------
# HTTP errors (replace werkzeug.exceptions; frappe routes on http_status_code)
# -------------------------------------------------------------------------


class HTTPError(Exception):
	"""Base for HTTP-status-mapped errors. handle_exception() reads
	http_status_code and renders the right error page / JSON."""

	http_status_code = 500
	description = "Internal Server Error"

	def __init__(self, description=None):
		super().__init__(description or self.description)
		if description is not None:
			self.description = description


class BadRequest(HTTPError):
	http_status_code = 400
	description = "Bad Request"


class Unauthorized(HTTPError):
	http_status_code = 401
	description = "Unauthorized"


class Forbidden(HTTPError):
	http_status_code = 403
	description = "Forbidden"


class NotFound(HTTPError):
	http_status_code = 404
	description = "Not Found"


class MethodNotAllowed(HTTPError):
	http_status_code = 405
	description = "Method Not Allowed"


class RequestEntityTooLarge(HTTPError):
	http_status_code = 413
	description = "Request Entity Too Large"


class TooManyRequests(HTTPError):
	http_status_code = 429
	description = "Too Many Requests"


# alias matching werkzeug's name; e.g. `except HTTPException` call sites
HTTPException = HTTPError


# -------------------------------------------------------------------------
# header parsing helpers
# -------------------------------------------------------------------------


def parse_options_header(value):
	"""'multipart/form-data; boundary=X' -> ('multipart/form-data', {'boundary': 'X'})"""
	if not value:
		return "", {}
	parts = value.split(";")
	main = parts[0].strip().lower()
	options = {}
	for part in parts[1:]:
		key, _, val = part.strip().partition("=")
		if key:
			options[key.lower()] = val.strip('"')
	return main, options


def parse_cookie_header(value: str) -> dict:
	cookies = {}
	for part in (value or "").split(";"):
		key, _, val = part.strip().partition("=")
		if key and key not in cookies:
			cookies[key] = unquote(val.strip('"'))
	return cookies


class RequestCacheControl:
	"""Request Cache-Control with attribute access (the used subset)."""

	def __init__(self, header: str | None):
		directives = {}
		for part in (header or "").split(","):
			key, _, val = part.strip().partition("=")
			if key:
				directives[key.lower().replace("-", "_")] = val.strip('"') or True
		self.no_cache = bool(directives.get("no_cache"))
		self.no_store = bool(directives.get("no_store"))
		self.max_age = directives.get("max_age")
		self._directives = directives

	def __getattr__(self, name):  # unknown directives -> None
		return self.__dict__.get("_directives", {}).get(name)


class LanguageAccept:
	"""Accept-Language, q-sorted; .values() yields language tags."""

	def __init__(self, header: str | None):
		entries = []
		for part in (header or "").split(","):
			lang, _, params = part.strip().partition(";")
			if not lang:
				continue
			quality = 1.0
			if params.strip().startswith("q="):
				try:
					quality = float(params.strip()[2:])
				except ValueError:
					pass
			entries.append((lang.strip(), quality))
		entries.sort(key=lambda e: e[1], reverse=True)
		self._entries = entries

	def values(self):
		return [lang for lang, _ in self._entries]

	def __iter__(self):
		return iter(self._entries)

	def __bool__(self):
		return bool(self._entries)


def cookie_date(value: datetime) -> str:
	if value.tzinfo is None:
		value = value.replace(tzinfo=UTC)
	return format_datetime(value.astimezone(UTC), usegmt=True)


def dump_cookie(
	key,
	value="",
	max_age=None,
	expires=None,
	path="/",
	domain=None,
	secure=False,
	httponly=False,
	samesite=None,
) -> str:
	"""Build a Set-Cookie header value (werkzeug-compatible subset). The
	callers URL-quote values themselves; quote defensively anyway."""
	value = quote(str(value), safe="!#$%&'()*+-./:<=>?@[]^_`{|}~")
	parts = [f"{key}={value}"]
	if expires is not None:
		if isinstance(expires, datetime):
			expires = cookie_date(expires)
		parts.append(f"Expires={expires}")
	if max_age is not None:
		parts.append(f"Max-Age={int(max_age)}")
	if domain:
		parts.append(f"Domain={domain}")
	if path:
		parts.append(f"Path={path}")
	if secure:
		parts.append("Secure")
	if httponly:
		parts.append("HttpOnly")
	if samesite:
		parts.append(f"SameSite={samesite}")
	return "; ".join(parts)


# -------------------------------------------------------------------------
# Request
# -------------------------------------------------------------------------

class Request:
	"""HTTP request built straight from the ASGI scope (werkzeug.Request
	compatible subset). Body parsing (form/files/json) is lazy and runs on
	whatever (pool) thread first touches it."""

	max_content_length: int | None = None

	def __init__(
		self,
		method="GET",
		path="/",
		query_string=b"",
		headers=None,
		body=None,
		scheme="http",
		remote_addr=None,
		host=None,
	):
		self.method = method.upper()
		self.path = path
		self.query_string = query_string if isinstance(query_string, bytes) else query_string.encode()
		self.headers = headers if isinstance(headers, Headers) else Headers(headers or {})
		self.scheme = scheme
		self.remote_addr = remote_addr
		self._host = host
		self._body_stream = body  # file-like positioned at 0, or None
		self._cached_data: bytes | None = None
		self._form = None
		self._files = None
		self._cookies = None

	# --- constructors -----------------------------------------------------

	@classmethod
	def from_scope(cls, scope, body_stream):
		"""Build from an ASGI HTTP scope + (spooled) body file."""
		raw_path = scope.get("raw_path")
		path = raw_path.split(b"?", 1)[0].decode("latin-1") if raw_path else scope["path"]
		path = unquote(path)
		headers = Headers()
		for key, value in scope["headers"]:
			headers.add(key.decode("latin-1").title(), value.decode("latin-1"))
		client = scope.get("client") or (None, None)
		return cls(
			method=scope["method"],
			path=path,
			query_string=scope.get("query_string", b""),
			headers=headers,
			body=body_stream,
			scheme=scope.get("scheme", "http"),
			remote_addr=client[0],
		)

	@classmethod
	def from_values(cls, method="GET", path="/", query_string=None, headers=None, data=None, **kwargs):
		"""Test/fake-request builder (replaces werkzeug EnvironBuilder usage
		in set_request). Supports the kwargs frappe actually passes."""
		if "?" in path and query_string is None:
			path, _, query_string = path.partition("?")
		body = None
		hdrs = Headers(headers or {})
		if data is not None:
			body_bytes = data.encode() if isinstance(data, str) else data
			body = io.BytesIO(body_bytes)
			hdrs.setdefault("Content-Length", str(len(body_bytes)))
		if content_type := kwargs.pop("content_type", None):
			hdrs.set("Content-Type", content_type)
		host = kwargs.pop("host", None)
		if host:
			hdrs.set("Host", host)
		kwargs.pop("environ_base", None)  # werkzeug compat, meaningless here
		return cls(
			method=method,
			path=path or "/",
			query_string=query_string or b"",
			headers=hdrs,
			body=body,
			host=host,
			**kwargs,
		)

	# --- url pieces ---------------------------------------------------------

	@property
	def host(self):
		return self._host or self.headers.get("Host") or "localhost"

	@property
	def host_url(self):
		return f"{self.scheme}://{self.host}/"

	url_root = host_url

	@property
	def base_url(self):
		return f"{self.scheme}://{self.host}{quote(self.path)}"

	@property
	def url(self):
		query = self.query_string.decode("latin-1")
		return self.base_url + (f"?{query}" if query else "")

	@property
	def full_path(self):
		return f"{self.path}?{self.query_string.decode('latin-1')}"

	@property
	def is_secure(self):
		return self.scheme == "https"

	# --- headers-derived ----------------------------------------------------

	@property
	def content_type(self):
		return self.headers.get("Content-Type", "")

	@property
	def mimetype(self):
		return parse_options_header(self.content_type)[0]

	@property
	def content_length(self):
		return self.headers.get("Content-Length", 0, type=int)

	@property
	def cookies(self):
		if self._cookies is None:
			self._cookies = parse_cookie_header(self.headers.get("Cookie", ""))
		return self._cookies

	@property
	def cache_control(self):
		return RequestCacheControl(self.headers.get("Cache-Control"))

	@property
	def accept_languages(self):
		return LanguageAccept(self.headers.get("Accept-Language"))

	# --- query args ---------------------------------------------------------

	@property
	def args(self):
		if not hasattr(self, "_args"):
			self._args = MultiDict(
				parse_qsl(self.query_string.decode("latin-1"), keep_blank_values=True)
			)
		return self._args

	# --- body ---------------------------------------------------------------

	def get_data(self, as_text=False):
		if self._cached_data is None:
			if self._form is not None:
				# form parsing consumed the stream
				self._cached_data = b""
			elif self._body_stream is None:
				self._cached_data = b""
			else:
				self._check_content_length()
				self._body_stream.seek(0)
				self._cached_data = self._body_stream.read()
		return self._cached_data.decode(errors="replace") if as_text else self._cached_data

	@property
	def data(self):
		return self.get_data()

	@property
	def is_json(self):
		return "application/json" in (self.content_type or "")

	@property
	def json(self):
		return self.get_json()

	def get_json(self, silent=False):
		try:
			return orjson.loads(self.get_data() or b"null")
		except orjson.JSONDecodeError:
			if silent:
				return None
			raise BadRequest("Invalid JSON body")

	def _check_content_length(self):
		if self.max_content_length is not None and self.content_length:
			if self.content_length > self.max_content_length:
				raise RequestEntityTooLarge

	def _parse_form_data(self):
		if self._form is not None:
			return
		self._form, self._files = MultiDict(), MultiDict()
		mimetype = self.mimetype
		# werkzeug parity: parsing is decided by content type, NOT method —
		# frappe's test clients send urlencoded bodies on GET (oauth flows)
		if self._body_stream is None:
			return
		self._check_content_length()
		self._body_stream.seek(0)

		if mimetype == "application/x-www-form-urlencoded":
			body = self._body_stream.read()
			self._cached_data = body
			for key, value in parse_qsl(body.decode(errors="replace"), keep_blank_values=True):
				self._form.add(key, value)

		elif mimetype == "multipart/form-data":
			from python_multipart import parse_form

			def on_field(field):
				self._form.add(
					field.field_name.decode(errors="replace"),
					(field.value or b"").decode(errors="replace"),
				)

			def on_file(file):
				file.file_object.seek(0)
				self._files.add(
					file.field_name.decode(errors="replace"),
					FileStorage(
						stream=file.file_object,
						filename=(file.file_name or b"").decode(errors="replace") or None,
						name=file.field_name.decode(errors="replace"),
					),
				)

			parse_form(
				{"Content-Type": self.content_type, "Content-Length": str(self.content_length or "")},
				self._body_stream,
				on_field,
				on_file,
			)

	@property
	def form(self):
		self._parse_form_data()
		return self._form

	@property
	def files(self):
		self._parse_form_data()
		return self._files

	@property
	def values(self):
		combined = MultiDict()
		for key, value in self.args.items(multi=True):
			combined.add(key, value)
		for key, value in self.form.items(multi=True):
			combined.add(key, value)
		return combined

	@property
	def environ(self):
		"""Legacy WSGI-shaped view of the request (kept for old tests; the
		server itself never builds an environ)."""
		return {
			"REQUEST_METHOD": self.method,
			"PATH_INFO": self.path,
			"QUERY_STRING": self.query_string.decode("latin-1"),
		}

	def close(self):
		if self._body_stream is not None:
			try:
				self._body_stream.close()
			except Exception:
				pass

	def __repr__(self):
		return f"<Request {self.method} {self.path!r}>"


# -------------------------------------------------------------------------
# Response
# -------------------------------------------------------------------------

HTTP_STATUS_PHRASES = {
	200: "OK",
	201: "Created",
	204: "No Content",
	206: "Partial Content",
	301: "Moved Permanently",
	302: "Found",
	303: "See Other",
	304: "Not Modified",
	307: "Temporary Redirect",
	308: "Permanent Redirect",
	400: "Bad Request",
	401: "Unauthorized",
	403: "Forbidden",
	404: "Not Found",
	405: "Method Not Allowed",
	413: "Request Entity Too Large",
	416: "Range Not Satisfiable",
	417: "Expectation Failed",
	429: "Too Many Requests",
	500: "Internal Server Error",
	503: "Service Unavailable",
	508: "Loop Detected",
}


class Response:
	"""HTTP response (werkzeug.Response compatible subset). Body is bytes,
	a str, an iterable of bytes, or a file: file responses set `file_path`
	(served by asgi.py with aiofiles + Range/conditional support)."""

	default_mimetype = "text/html"

	def __init__(
		self,
		response=None,
		status=None,
		headers=None,
		mimetype=None,
		content_type=None,
		direct_passthrough=False,
	):
		self.headers = headers if isinstance(headers, Headers) else Headers(headers or {})
		self.status_code = 200
		if status is not None:
			self.status = status
		if content_type is None:
			content_type = mimetype or self.default_mimetype
			if content_type.startswith("text/") and "charset" not in content_type:
				content_type += "; charset=utf-8"
		self.headers.setdefault("Content-Type", content_type)
		self.direct_passthrough = direct_passthrough
		self.file_path = None  # set by send_file for aiofiles streaming
		self.conditional = False
		self.download_name = None
		self._iterable = None
		self._data = b""
		if response is not None:
			if isinstance(response, str | bytes):
				self.set_data(response)
			else:
				self._iterable = response

	# --- status -------------------------------------------------------------

	@property
	def status_code(self):
		return self._status_code

	@status_code.setter
	def status_code(self, value):
		# werkzeug parity: coerce — website code assigns strings ("301")
		self._status_code = int(value)

	@property
	def status(self):
		return f"{self.status_code} {HTTP_STATUS_PHRASES.get(self.status_code, 'UNKNOWN')}"

	@status.setter
	def status(self, value):
		if isinstance(value, int):
			self.status_code = value
		else:
			self.status_code = int(str(value).split(" ", 1)[0])

	# --- body ---------------------------------------------------------------

	def get_data(self, as_text=False):
		if self._iterable is not None:
			chunks = []
			for chunk in self._iterable:
				chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
			self._data = b"".join(chunks)
			close = getattr(self._iterable, "close", None)
			if close:
				close()
			self._iterable = None
		return self._data.decode(errors="replace") if as_text else self._data

	def set_data(self, value):
		self._iterable = None
		self._data = value.encode() if isinstance(value, str) else bytes(value)
		# werkzeug parity: assigning a body keeps Content-Length current
		self.headers.set("Content-Length", str(len(self._data)))

	data = property(get_data, set_data)

	@property
	def response(self):  # werkzeug exposes body parts list
		return self._iterable if self._iterable is not None else [self._data]

	def iter_chunks(self):
		"""Iterate body chunks (bytes). For non-file responses only."""
		if self._iterable is not None:
			for chunk in self._iterable:
				yield chunk.encode() if isinstance(chunk, str) else chunk
		elif self._data:
			yield self._data

	def close(self):
		close = getattr(self._iterable, "close", None)
		if close:
			close()

	# --- header conveniences --------------------------------------------------

	@property
	def mimetype(self):
		content_type = self.headers.get("Content-Type", "")
		return content_type.split(";", 1)[0].strip()

	@mimetype.setter
	def mimetype(self, value):
		if value.startswith("text/") or value in ("application/json",):
			value += "; charset=utf-8"
		self.headers.set("Content-Type", value)

	@property
	def content_type(self):
		return self.headers.get("Content-Type")

	@content_type.setter
	def content_type(self, value):
		self.headers.set("Content-Type", value)

	def set_cookie(
		self,
		key,
		value="",
		max_age=None,
		expires=None,
		path="/",
		domain=None,
		secure=False,
		httponly=False,
		samesite=None,
	):
		self.headers.add(
			"Set-Cookie",
			dump_cookie(
				key,
				value,
				max_age=max_age,
				expires=expires,
				path=path,
				domain=domain,
				secure=secure,
				httponly=httponly,
				samesite=samesite,
			),
		)

	def delete_cookie(self, key, path="/", domain=None):
		self.set_cookie(key, "", expires=datetime(1970, 1, 1, tzinfo=UTC), path=path, domain=domain)

	def __call__(self, environ, start_response):
		"""Minimal WSGI compatibility — only the legacy werkzeug test-client
		path uses this (until Phase 20.6 swaps it for httpx/ASGI). The real
		server path is asgi.py sending this object natively."""
		if self.file_path:
			with open(self.file_path, "rb") as f:
				body = f.read()
		else:
			body = self.get_data()
		if "Content-Length" not in self.headers:
			self.headers.set("Content-Length", str(len(body)))
		start_response(self.status, self.headers.items())
		return [body]

	def __repr__(self):
		return f"<Response {self.status}>"


def redirect(location, code=302) -> Response:
	"""Replacement for werkzeug.utils.redirect."""
	html = f'<!doctype html>\n<title>Redirecting...</title>\n<a href="{location}">Click here</a>\n'
	response = Response(html, status=code, mimetype="text/html")
	# match werkzeug: %-encode non-latin1 in Location
	response.headers.set("Location", quote(location, safe="/:?&#%+=@$!*'(),;~[]"))
	return response


def send_file(path, conditional=True, as_attachment=False, download_name=None, environ=None) -> Response:
	"""File response served by asgi.py via aiofiles with Range + 304 support
	(replacement for werkzeug.utils.send_file; `environ` accepted+ignored)."""
	path = os.fspath(path)
	if not os.path.isfile(path):
		raise NotFound
	download_name = download_name or os.path.basename(path)
	content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
	response = Response(content_type=content_type)
	response.file_path = path
	response.conditional = conditional
	response.download_name = download_name
	if as_attachment:
		response.headers.set("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name)}")
	else:
		response.headers.set("Content-Disposition", f"inline; filename*=UTF-8''{quote(download_name)}")
	return response


# -------------------------------------------------------------------------
# routing (replaces the werkzeug.routing subset frappe uses)
# -------------------------------------------------------------------------


class RequestRedirect(Exception):
	"""Trailing-slash redirect raised during matching (werkzeug name kept)."""

	def __init__(self, new_url, code=308):
		super().__init__(new_url)
		self.new_url = new_url
		self.code = code


_CONVERTER_RE = {
	"default": r"[^/]+",
	"string": r"[^/]+",
	"path": r".+",
	"int": r"\d+",
}

_RULE_PART_RE = re.compile(r"<(?:(?P<converter>[a-z]+):)?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)>")


class Rule:
	def __init__(self, rule: str, endpoint=None, methods=None, defaults=None):
		self.rule = rule
		self.endpoint = endpoint
		self.methods = {m.upper() for m in methods} if methods else None
		if self.methods and "GET" in self.methods:
			self.methods.add("HEAD")
		self.defaults = defaults or {}
		self._int_args = set()
		self._regex = self._compile(rule)

	def _compile(self, rule: str):
		pattern = ""
		index = 0
		for match in _RULE_PART_RE.finditer(rule):
			pattern += re.escape(rule[index : match.start()])
			converter = match.group("converter") or "default"
			name = match.group("name")
			if converter == "int":
				self._int_args.add(name)
			pattern += f"(?P<{name}>{_CONVERTER_RE.get(converter, _CONVERTER_RE['default'])})"
			index = match.end()
		pattern += re.escape(rule[index:])
		return re.compile(f"^{pattern}$")

	def match(self, path: str):
		match = self._regex.match(path)
		if match is None:
			return None
		args = {k: unquote(v) for k, v in match.groupdict().items()}
		for name in self._int_args:
			args[name] = int(args[name])
		return {**self.defaults, **args}


class Submount:
	def __init__(self, prefix: str, rules):
		self.prefix = prefix.rstrip("/")
		self.rules = rules

	def expand(self):
		for rule in self.rules:
			yield Rule(
				self.prefix + rule.rule,
				endpoint=rule.endpoint,
				methods=sorted(rule.methods) if rule.methods else None,
				defaults=rule.defaults,
			)


class Map:
	"""URL map. `strict_slashes=False` treats trailing slashes as optional;
	the default (strict) raises RequestRedirect when only the slash variant
	matches — mirroring the werkzeug behavior path_resolver depends on."""

	def __init__(self, rules=None, strict_slashes=True, merge_slashes=None):
		self.strict_slashes = strict_slashes
		self._rules: list[Rule] = []
		for rule in rules or []:
			self.add(rule)

	def add(self, rule):
		if isinstance(rule, Submount):
			self._rules.extend(rule.expand())
		else:
			self._rules.append(rule)

	def match(self, path: str, method: str = "GET"):
		"""Return (endpoint, args). Raises NotFound / MethodNotAllowed /
		RequestRedirect (strict mode trailing-slash fixup)."""
		method = method.upper()
		method_misses = False
		candidates = [path]
		if not self.strict_slashes:
			candidates.append(path[:-1] if path.endswith("/") else path + "/")
		for candidate in candidates:
			for rule in self._rules:
				args = rule.match(candidate)
				if args is None:
					continue
				if rule.methods and method not in rule.methods:
					method_misses = True
					continue
				return rule.endpoint, args
		if self.strict_slashes and not path.endswith("/"):
			for rule in self._rules:
				if rule.match(path + "/") is not None:
					raise RequestRedirect(path + "/")
		if method_misses:
			raise MethodNotAllowed
		raise NotFound

	def bind(self, *args, **kwargs):
		return self

	bind_to_environ = bind
