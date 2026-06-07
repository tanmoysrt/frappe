"""Phase 12: async-primary SMTP (aiosmtplib via bridge) + async outbound HTTP.

SMTP runs against a real local aiosmtpd server — the full wire path
(connect/EHLO/MAIL/RCPT/DATA/QUIT) through the exact session facade
EmailQueue.send uses. HTTP runs against a local http.server thread.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import frappe
from frappe.email.aio import BridgedSMTPSession
from frappe.email.smtp import SMTPServer
from frappe.integrations.aio import get_async_http_client, make_request_async
from frappe.tests import AsyncUnitTestCase, IntegrationTestCase


class _MailCollector:
	def __init__(self):
		self.envelopes = []

	async def handle_DATA(self, server, session, envelope):
		self.envelopes.append(envelope)
		return "250 Message accepted for delivery"


class TestAsyncSMTP(IntegrationTestCase):
	def setUp(self):
		import socket

		from aiosmtpd.controller import Controller

		super().setUp()
		# Controller's readiness probe dials the configured port, so port=0
		# (ephemeral) doesn't work — reserve a free one first
		with socket.socket() as s:
			s.bind(("127.0.0.1", 0))
			self.port = s.getsockname()[1]
		self.collector = _MailCollector()
		self.controller = Controller(self.collector, hostname="127.0.0.1", port=self.port)
		self.controller.start()
		self.addCleanup(self.controller.stop)

	def _server(self):
		return SMTPServer(server="127.0.0.1", port=self.port, use_tls=0, use_ssl=0)

	def test_sendmail_round_trip(self):
		srv = self._server()
		session = srv.session
		self.assertIsInstance(session, BridgedSMTPSession)  # async-primary path
		refused = session.sendmail(
			"sender@example.com", ["rcpt@example.com"], b"Subject: hello\r\n\r\nbridged body"
		)
		self.assertEqual(refused, {})
		self.assertEqual(len(self.collector.envelopes), 1)
		envelope = self.collector.envelopes[0]
		self.assertEqual(envelope.mail_from, "sender@example.com")
		self.assertEqual(envelope.rcpt_tos, ["rcpt@example.com"])
		self.assertIn(b"bridged body", envelope.content)
		srv.quit()

	def test_session_revival_and_extensions(self):
		srv = self._server()
		session = srv.session
		# the surfaces EmailQueue.send actually touches
		self.assertTrue(srv.is_session_active())  # noop()[0] == 250 through the facade
		self.assertIsInstance(session.esmtp_features, dict)
		self.assertEqual(session.has_extn("8BITMIME"), "8bitmime" in session.esmtp_features)
		self.assertIs(srv.session, session)  # healthy session is reused
		srv.quit()
		self.assertFalse(srv.is_session_active())
		revived = srv.session  # dead session is replaced transparently
		self.assertIsNot(revived, session)
		srv.quit()

	def test_connection_refused_raises_config_error(self):
		srv = SMTPServer(server="127.0.0.1", port=1, use_tls=0, use_ssl=0)  # nothing listens on 1
		self.assertRaises(frappe.ValidationError, lambda: srv.session)


class _EchoHandler(BaseHTTPRequestHandler):
	def _reply(self, payload: bytes, content_type="application/json"):
		self.send_response(200)
		self.send_header("content-type", content_type)
		self.send_header("content-length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def do_GET(self):
		self._reply(b'{"ok": true, "path": "%s"}' % self.path.encode())

	def do_POST(self):
		body = self.rfile.read(int(self.headers.get("content-length", 0)))
		self._reply(json.dumps({"echo": json.loads(body)}).encode())

	def log_message(self, *args):
		pass


class TestAsyncHTTP(AsyncUnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.httpd = HTTPServer(("127.0.0.1", 0), _EchoHandler)
		cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
		threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

	@classmethod
	def tearDownClass(cls):
		cls.httpd.shutdown()
		super().tearDownClass()

	async def test_get_returns_parsed_json(self):
		result = await make_request_async("GET", f"{self.base}/ping")
		self.assertEqual(result, {"ok": True, "path": "/ping"})
		self.assertEqual(frappe.flags.integration_request.status_code, 200)

	async def test_post_json_round_trip(self):
		result = await make_request_async("POST", f"{self.base}/echo", json={"a": 1})
		self.assertEqual(result, {"echo": {"a": 1}})

	async def test_client_shared_per_loop(self):
		self.assertIs(get_async_http_client(), get_async_http_client())

	async def test_http_error_raises(self):
		import httpx

		with self.assertRaises(httpx.HTTPStatusError):
			await make_request_async("DELETE", f"{self.base}/nope")  # handler has no do_DELETE -> 501
