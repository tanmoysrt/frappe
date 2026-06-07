"""Phase 20: frappe-native HTTP primitives (frappe/http.py).

Cookie/header formatting cases mirror werkzeug's test vectors so the
werkzeug -> native swap is invisible to clients.
"""

import io
from datetime import UTC, datetime

from frappe.http import (
	FileStorage,
	Headers,
	Map,
	MethodNotAllowed,
	MultiDict,
	NotFound,
	Request,
	RequestRedirect,
	Response,
	Rule,
	Submount,
	dump_cookie,
	parse_options_header,
	redirect,
)
from frappe.tests import UnitTestCase


class TestHeaders(UnitTestCase):
	def test_case_insensitive_get_set(self):
		h = Headers({"Content-Type": "text/html"})
		self.assertEqual(h.get("content-type"), "text/html")
		h["X-Custom"] = "1"
		self.assertIn("x-custom", h)
		del h["X-CUSTOM"]
		self.assertNotIn("x-custom", h)

	def test_multi_value(self):
		h = Headers()
		h.add("Set-Cookie", "a=1")
		h.add("Set-Cookie", "b=2")
		self.assertEqual(h.getlist("set-cookie"), ["a=1", "b=2"])
		self.assertEqual(len(h), 2)
		h.set("Set-Cookie", "c=3")  # set replaces ALL
		self.assertEqual(h.getlist("set-cookie"), ["c=3"])

	def test_add_with_options(self):
		h = Headers()
		h.add("Content-Disposition", "attachment", filename="report.csv")
		self.assertEqual(h.get("Content-Disposition"), "attachment; filename=report.csv")
		h.set("Content-Disposition", "attachment", filename="my report.csv")
		self.assertEqual(h.get("Content-Disposition"), 'attachment; filename="my report.csv"')
		h.set("Content-Disposition", "attachment", filename="रिपोर्ट.csv")
		# werkzeug parity: non-ascii filenames stay quoted (no RFC 5987 form)
		self.assertEqual(h.get("Content-Disposition"), 'attachment; filename="रिपोर्ट.csv"')

	def test_iter_yields_pairs(self):
		h = Headers({"A": "1", "B": "2"})
		self.assertEqual(dict(h), {"A": "1", "B": "2"})

	def test_update_and_setdefault(self):
		h = Headers({"A": "1"})
		h.update({"A": "9", "B": "2"})
		self.assertEqual(h.get("A"), "9")
		h.setdefault("A", "0")
		self.assertEqual(h.get("A"), "9")
		self.assertEqual(h.get("Missing", "x"), "x")
		self.assertEqual(h.get("B", type=int), 2)


class TestMultiDict(UnitTestCase):
	def test_first_value_semantics(self):
		d = MultiDict([("a", "1"), ("a", "2"), ("b", "3")])
		self.assertEqual(d["a"], "1")
		self.assertEqual(d.getlist("a"), ["1", "2"])
		self.assertEqual(d.get("b"), "3")
		self.assertEqual(d.get("b", type=int), 3)
		self.assertEqual(dict(d.items()), {"a": "1", "b": "3"})
		self.assertEqual(d.to_dict(), {"a": "1", "b": "3"})

	def test_plain_dict_update_takes_first(self):
		d = MultiDict([("a", "1"), ("a", "2")])
		plain = {}
		plain.update(d)
		self.assertEqual(plain, {"a": "1"})

	def test_setitem_replaces(self):
		d = MultiDict([("a", "1"), ("a", "2")])
		d["a"] = "9"
		self.assertEqual(d.getlist("a"), ["9"])


class TestCookie(UnitTestCase):
	def test_basic(self):
		self.assertEqual(
			dump_cookie("sid", "abc123", httponly=True, samesite="Lax"),
			"sid=abc123; Path=/; HttpOnly; SameSite=Lax",
		)

	def test_expires_and_max_age(self):
		cookie = dump_cookie("k", "v", expires=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC), max_age=3600)
		self.assertIn("Expires=Thu, 15 Jan 2026 12:00:00 GMT", cookie)
		self.assertIn("Max-Age=3600", cookie)

	def test_secure(self):
		self.assertIn("Secure", dump_cookie("k", "v", secure=True))

	def test_response_set_cookie(self):
		r = Response()
		r.set_cookie("a", "1")
		r.set_cookie("b", "2")
		self.assertEqual(len(r.headers.getlist("Set-Cookie")), 2)


class TestRequest(UnitTestCase):
	def _scope(self, **over):
		scope = {
			"type": "http",
			"method": "GET",
			"path": "/api/method/ping",
			"raw_path": b"/api/method/ping",
			"query_string": b"a=1&a=2&b=x%20y",
			"headers": [
				(b"host", b"test.local:8001"),
				(b"cookie", b"sid=abc; user_id=admin%40site"),
				(b"content-type", b"application/json"),
				(b"accept-language", b"de;q=0.8, en, fr;q=0.5"),
				(b"cache-control", b"no-cache"),
			],
			"client": ("10.0.0.5", 1234),
			"scheme": "https",
		}
		scope.update(over)
		return scope

	def test_from_scope_basics(self):
		r = Request.from_scope(self._scope(), io.BytesIO(b""))
		self.assertEqual(r.method, "GET")
		self.assertEqual(r.path, "/api/method/ping")
		self.assertEqual(r.host, "test.local:8001")
		self.assertEqual(r.scheme, "https")
		self.assertTrue(r.is_secure)
		self.assertEqual(r.remote_addr, "10.0.0.5")
		self.assertEqual(r.url, "https://test.local:8001/api/method/ping?a=1&a=2&b=x%20y")
		self.assertEqual(r.host_url, "https://test.local:8001/")

	def test_args(self):
		r = Request.from_scope(self._scope(), io.BytesIO(b""))
		self.assertEqual(r.args["a"], "1")
		self.assertEqual(r.args.getlist("a"), ["1", "2"])
		self.assertEqual(r.args["b"], "x y")

	def test_cookies(self):
		r = Request.from_scope(self._scope(), io.BytesIO(b""))
		self.assertEqual(r.cookies["sid"], "abc")
		self.assertEqual(r.cookies["user_id"], "admin@site")

	def test_accept_languages_q_sorted(self):
		r = Request.from_scope(self._scope(), io.BytesIO(b""))
		self.assertEqual(r.accept_languages.values(), ["en", "de", "fr"])

	def test_cache_control(self):
		r = Request.from_scope(self._scope(), io.BytesIO(b""))
		self.assertTrue(r.cache_control.no_cache)
		self.assertFalse(r.cache_control.no_store)

	def test_json_body(self):
		body = b'{"x": 1}'
		scope = self._scope(method="POST")
		scope["headers"].append((b"content-length", str(len(body)).encode()))
		r = Request.from_scope(scope, io.BytesIO(body))
		self.assertTrue(r.is_json)
		self.assertEqual(r.json, {"x": 1})
		self.assertEqual(r.get_data(as_text=True), '{"x": 1}')

	def test_urlencoded_form(self):
		body = b"name=Hello+World&tag=a&tag=b"
		scope = self._scope(method="POST")
		scope["headers"] = [
			(b"host", b"test.local"),
			(b"content-type", b"application/x-www-form-urlencoded"),
			(b"content-length", str(len(body)).encode()),
		]
		r = Request.from_scope(scope, io.BytesIO(body))
		self.assertEqual(r.form["name"], "Hello World")
		self.assertEqual(r.form.getlist("tag"), ["a", "b"])
		self.assertEqual(r.files, {})

	def test_multipart_form(self):
		body = (
			b"--BOUND\r\n"
			b'Content-Disposition: form-data; name="field1"\r\n\r\n'
			b"value1\r\n"
			b"--BOUND\r\n"
			b'Content-Disposition: form-data; name="file"; filename="hello.txt"\r\n'
			b"Content-Type: text/plain\r\n\r\n"
			b"file-content-here\r\n"
			b"--BOUND--\r\n"
		)
		scope = self._scope(method="POST")
		scope["headers"] = [
			(b"host", b"test.local"),
			(b"content-type", b"multipart/form-data; boundary=BOUND"),
			(b"content-length", str(len(body)).encode()),
		]
		r = Request.from_scope(scope, io.BytesIO(body))
		self.assertEqual(r.form["field1"], "value1")
		storage = r.files["file"]
		self.assertIsInstance(storage, FileStorage)
		self.assertEqual(storage.filename, "hello.txt")
		self.assertEqual(storage.stream.read(), b"file-content-here")

	def test_max_content_length(self):
		from frappe.http import RequestEntityTooLarge

		body = b"x" * 100
		scope = self._scope(method="POST")
		scope["headers"] = [(b"content-length", b"100"), (b"content-type", b"text/plain")]
		r = Request.from_scope(scope, io.BytesIO(body))
		r.max_content_length = 10
		self.assertRaises(RequestEntityTooLarge, r.get_data)

	def test_from_values(self):
		r = Request.from_values(method="GET", path="/about?x=1")
		self.assertEqual(r.path, "/about")
		self.assertEqual(r.args["x"], "1")
		r2 = Request.from_values(method="POST", path="/", data=b"a=1", content_type="application/x-www-form-urlencoded")
		self.assertEqual(r2.form["a"], "1")

	def test_path_is_percent_decoded(self):
		scope = self._scope(path="/files/a b.txt", raw_path=b"/files/a%20b.txt")
		r = Request.from_scope(scope, io.BytesIO(b""))
		self.assertEqual(r.path, "/files/a b.txt")


class TestResponse(UnitTestCase):
	def test_defaults(self):
		r = Response()
		self.assertEqual(r.status_code, 200)
		self.assertEqual(r.status, "200 OK")
		self.assertEqual(r.headers.get("Content-Type"), "text/html; charset=utf-8")

	def test_data_and_mimetype(self):
		r = Response()
		r.mimetype = "application/json"
		r.data = b'{"ok": true}'
		self.assertEqual(r.headers.get("Content-Type"), "application/json; charset=utf-8")
		self.assertEqual(r.get_data(as_text=True), '{"ok": true}')

	def test_str_body_and_status(self):
		r = Response("hello", status=201, content_type="text/plain")
		self.assertEqual(r.get_data(), b"hello")
		self.assertEqual(r.status_code, 201)
		r.status = "404 NOT FOUND"
		self.assertEqual(r.status_code, 404)

	def test_iterable_body(self):
		r = Response(iter([b"a", b"b", b"c"]))
		self.assertEqual(list(r.iter_chunks()), [b"a", b"b", b"c"])
		r2 = Response(iter([b"a", "b"]))
		self.assertEqual(r2.get_data(), b"ab")

	def test_redirect(self):
		r = redirect("/login?redirect-to=%2Fapp")
		self.assertEqual(r.status_code, 302)
		self.assertEqual(r.headers.get("Location"), "/login?redirect-to=%2Fapp")


class TestRouting(UnitTestCase):
	def _map(self):
		v1 = [
			Rule("/method/<path:method>", endpoint="rpc"),
			Rule("/resource/<doctype>", methods=["GET"], endpoint="list"),
			Rule("/resource/<doctype>", methods=["POST"], endpoint="create"),
			Rule("/resource/<doctype>/<path:name>/", methods=["GET"], endpoint="read"),
			Rule("/resource/<doctype>/<path:name>/", methods=["DELETE"], endpoint="delete"),
		]
		return Map(
			[Submount("/api", v1), Submount("/api/v1", v1)],
			strict_slashes=False,
			merge_slashes=False,
		)

	def test_method_routing(self):
		m = self._map()
		self.assertEqual(m.match("/api/method/frappe.ping", "GET"), ("rpc", {"method": "frappe.ping"}))
		self.assertEqual(
			m.match("/api/method/a.b/extra", "POST"), ("rpc", {"method": "a.b/extra"})
		)
		self.assertEqual(m.match("/api/resource/ToDo", "GET"), ("list", {"doctype": "ToDo"}))
		self.assertEqual(m.match("/api/resource/ToDo", "POST"), ("create", {"doctype": "ToDo"}))
		# optional trailing slash both ways (strict_slashes=False)
		self.assertEqual(
			m.match("/api/resource/ToDo/TASK-001", "GET"),
			("read", {"doctype": "ToDo", "name": "TASK-001"}),
		)
		self.assertEqual(
			m.match("/api/v1/resource/ToDo/TASK-001/", "DELETE"),
			("delete", {"doctype": "ToDo", "name": "TASK-001"}),
		)

	def test_head_allowed_on_get(self):
		self.assertEqual(self._map().match("/api/resource/ToDo", "HEAD")[0], "list")

	def test_not_found_and_method_not_allowed(self):
		m = self._map()
		self.assertRaises(NotFound, m.match, "/api/unknown", "GET")
		self.assertRaises(MethodNotAllowed, m.match, "/api/resource/ToDo", "PUT")

	def test_percent_decoding_of_args(self):
		m = self._map()
		self.assertEqual(
			m.match("/api/resource/ToDo/TASK%20001", "GET")[1]["name"], "TASK 001"
		)

	def test_defaults_and_strict_slash_redirect(self):
		m = Map([Rule("/projects/", endpoint="projects", defaults={"page": 1})])
		self.assertEqual(m.match("/projects/", "GET"), ("projects", {"page": 1}))
		with self.assertRaises(RequestRedirect) as ctx:
			m.match("/projects", "GET")
		self.assertEqual(ctx.exception.new_url, "/projects/")

	def test_int_converter(self):
		m = Map([Rule("/page/<int:num>", endpoint="page")], strict_slashes=False)
		self.assertEqual(m.match("/page/42", "GET"), ("page", {"num": 42}))
		self.assertRaises(NotFound, m.match, "/page/abc", "GET")


class TestParseOptionsHeader(UnitTestCase):
	def test_basic(self):
		main, options = parse_options_header("multipart/form-data; boundary=XyZ")
		self.assertEqual(main, "multipart/form-data")
		self.assertEqual(options["boundary"], "XyZ")
		main, options = parse_options_header('text/html; charset="utf-8"')
		self.assertEqual(options["charset"], "utf-8")
		self.assertEqual(parse_options_header(None), ("", {}))
