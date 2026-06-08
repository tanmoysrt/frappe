"""Phase 26.3: lock the lean-backend predicate.

`frappe.serve._lean_backends` is now load-bearing in two places — `_reexec`
picks the glibc allocator (Phase 25.2) and `serve()` caps the thread pool
(Phase 24.10) off the SAME predicate. 26.3 de-duplicated the two copies into this
one helper; this test pins its truth table so the shared rule can't silently
drift.

Importing `frappe.serve` is cheap and side-effect-free: its module body is plain
stdlib imports + function defs (the re-exec / uvicorn runtime only run when
`main()` / `serve()` are called), so no server starts here.
"""

from frappe.serve import _lean_backends
from frappe.tests import UnitTestCase


class TestLeanBackends(UnitTestCase):
	def test_truth_table(self):
		# (config, expected) — lean == sqlite DB + memory/empty cache + sqlite/empty queue
		cases = [
			# bare sqlite: cache/queue unset -> the lean defaults
			({"db_type": "sqlite"}, True),
			# every lean key explicit
			({"db_type": "sqlite", "cache_backend": "memory", "queue_backend": "sqlite"}, True),
			# empty strings count as unset
			({"db_type": "sqlite", "cache_backend": "", "queue_backend": ""}, True),
			# any external backend breaks lean
			({"db_type": "mariadb"}, False),
			({"db_type": "sqlite", "cache_backend": "redis"}, False),
			({"db_type": "sqlite", "queue_backend": "rq"}, False),
			# empty / missing db_type is not lean
			({}, False),
		]
		for conf, expected in cases:
			with self.subTest(conf=conf):
				self.assertEqual(_lean_backends(conf), expected)
