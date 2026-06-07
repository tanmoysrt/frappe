"""Phase 24.0: import-budget regression pin.

Imports `frappe` in a FRESH subprocess (so this test process's already-loaded
modules don't pollute the count) and asserts:

  1. the module count stays under a ceiling — catches a new eager dependency
     sneaking onto the `import frappe` path,
  2. a denylist of heavy/optional modules is absent after a plain import.

pydantic is intentionally NOT on the denylist — it stays eager (hot validation
path). rq + redis ARE on the denylist: Phase 24.1 made the backend imports lazy
(enqueue/enqueue_doc via PEP 562, monitor/realtime/global_search function-level),
so a light site on the sqlite queue + in-process cache never pulls them.
"""

import json
import subprocess
import sys

from frappe.tests import UnitTestCase

# Ceiling for `len(sys.modules)` after `import frappe` (post-24.1: 460; baseline
# was 561). Headroom catches a big new dependency without flapping on minor churn.
MODULE_BUDGET = 520

# Heavy/optional modules that must NOT be pulled by a plain `import frappe`.
# rq/redis: lazy since 24.1. pydantic is intentionally absent (kept eager).
STRICT_DENYLIST = (
	"rq",
	"redis",
	"werkzeug.serving",
	"IPython",
	"sentry_sdk",
	"bs4",
	"openpyxl",
)

_PROBE = (
	"import sys, json\n"
	"import frappe\n"
	"print(json.dumps({'modules': len(sys.modules), 'loaded': list(sys.modules)}))\n"
)


def _import_frappe_fresh() -> dict:
	result = subprocess.run(
		[sys.executable, "-c", _PROBE],
		capture_output=True,
		text=True,
		timeout=120,
	)
	if result.returncode:
		raise AssertionError(f"`import frappe` failed rc={result.returncode}:\n{result.stderr}")
	return json.loads(result.stdout.strip().splitlines()[-1])


class TestImportBudget(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.probe = _import_frappe_fresh()

	def test_module_count_under_budget(self):
		count = self.probe["modules"]
		self.assertLessEqual(
			count,
			MODULE_BUDGET,
			f"`import frappe` loads {count} modules, over budget {MODULE_BUDGET} — "
			"a new eager dependency crept in.",
		)

	def test_strict_denylist_absent(self):
		loaded = set(self.probe["loaded"])
		leaked = sorted(m for m in STRICT_DENYLIST if m in loaded)
		self.assertEqual(leaked, [], f"unexpected eager imports: {leaked}")
