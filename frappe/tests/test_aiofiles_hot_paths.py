"""Phase 4: aiofiles in hot paths (frappe/asgi.py body buffering).

The only file I/O that ran on the event loop was request-body buffering:
SpooledTemporaryFile wrote spilled chunks synchronously on the loop. Now
small bodies stay in a bytearray -> BytesIO (no temp-file machinery at all),
and bodies over _SPOOL_MAX spill to a temp file written through aiofiles on
the loop's default executor. Sync call-sites (site config reads, file writes
in handlers) are untouched — they already run in pool threads since Phase 1.
"""

import io

from frappe.asgi import _SPOOL_MAX, _read_body
from frappe.tests import AsyncUnitTestCase


def make_receive(*messages):
	"""Fake ASGI receive() yielding the given messages in order."""
	queue = list(messages)

	async def receive():
		return queue.pop(0)

	return receive


def body_msg(data, more=False):
	return {"type": "http.request", "body": data, "more_body": more}


class TestReadBody(AsyncUnitTestCase):
	async def test_small_body_stays_in_memory(self):
		body, size = await _read_body(make_receive(body_msg(b"hello")))
		self.assertIsInstance(body, io.BytesIO)
		self.assertEqual(size, 5)
		self.assertEqual(body.read(), b"hello")
		body.close()

	async def test_empty_body(self):
		body, size = await _read_body(make_receive(body_msg(b"")))
		self.assertIsInstance(body, io.BytesIO)
		self.assertEqual(size, 0)
		self.assertEqual(body.read(), b"")
		body.close()

	async def test_multi_chunk_small_body(self):
		body, size = await _read_body(
			make_receive(body_msg(b"chunk1-", more=True), body_msg(b"chunk2"))
		)
		self.assertEqual(size, 13)
		self.assertEqual(body.read(), b"chunk1-chunk2")
		body.close()

	async def test_big_body_spills_to_disk(self):
		chunk = b"x" * (_SPOOL_MAX // 2)
		body, size = await _read_body(
			make_receive(
				body_msg(chunk, more=True),
				body_msg(chunk, more=True),
				body_msg(b"tail"),
			)
		)
		self.assertNotIsInstance(body, io.BytesIO)
		body.fileno()  # real on-disk file
		self.assertEqual(size, len(chunk) * 2 + 4)
		self.assertEqual(body.tell(), 0)  # rewound, ready for wsgi.input
		data = body.read()
		self.assertEqual(len(data), size)
		self.assertTrue(data.endswith(b"xtail"))
		body.close()

	async def test_spilled_chunks_arrive_in_order(self):
		# first chunk overflows the buffer; later chunks take the aiofiles path
		big = b"a" * (_SPOOL_MAX + 1)
		body, size = await _read_body(
			make_receive(body_msg(big, more=True), body_msg(b"b", more=True), body_msg(b"c"))
		)
		self.assertEqual(size, len(big) + 2)
		body.seek(size - 3)
		self.assertEqual(body.read(), b"abc")
		body.close()

	async def test_disconnect_returns_none(self):
		result = await _read_body(
			make_receive(body_msg(b"partial", more=True), {"type": "http.disconnect"})
		)
		self.assertIsNone(result)

	async def test_disconnect_after_spill_closes_temp_file(self):
		big = b"y" * (_SPOOL_MAX + 1)
		result = await _read_body(
			make_receive(body_msg(big, more=True), {"type": "http.disconnect"})
		)
		self.assertIsNone(result)  # temp file closed inside _read_body
