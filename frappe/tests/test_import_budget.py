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

Phase 25.0 adds a SECOND probe importing `frappe.app` (the ASGI request module +
its boot-preload block). The preload block used to drag rq/redis/bs4/requests/
posthog/markdownify in transitively; 25.0 made all of those function-level. The
same STRICT denylist must hold for `import frappe.app`, with its own (larger)
module budget.
"""

import json
import subprocess
import sys

from frappe.tests import UnitTestCase

# Ceiling for `len(sys.modules)` after `import frappe` (post-24.1: 460; baseline
# was 561). Headroom catches a big new dependency without flapping on minor churn.
MODULE_BUDGET = 520

# Ceiling after `import frappe.app` (post-25.0: 1009; pre-25.0 was 1457). This
# pulls the whole request/boot graph, so it's larger — but the heavies below must
# still be absent.
APP_MODULE_BUDGET = 1100

# Heavy/optional modules that must NOT be pulled by a plain `import frappe`
# OR by `import frappe.app`. rq/redis lazy since 24.1; bs4/requests/posthog/
# markdownify/num2words made lazy on the frappe.app path in 25.0. pydantic is
# intentionally absent (kept eager).
STRICT_DENYLIST = (
	"rq",
	"redis",
	"werkzeug.serving",
	"IPython",
	"sentry_sdk",
	"bs4",
	"openpyxl",
	"requests",
	"posthog",
	"markdownify",
	"num2words",
)


def _import_fresh(module: str) -> dict:
	probe = (
		"import sys, json\n"
		f"import {module}\n"
		"print(json.dumps({'modules': len(sys.modules), 'loaded': list(sys.modules)}))\n"
	)
	result = subprocess.run(
		[sys.executable, "-c", probe],
		capture_output=True,
		text=True,
		timeout=120,
	)
	if result.returncode:
		raise AssertionError(f"`import {module}` failed rc={result.returncode}:\n{result.stderr}")
	return json.loads(result.stdout.strip().splitlines()[-1])


class TestImportBudget(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.probe = _import_fresh("frappe")
		cls.app_probe = _import_fresh("frappe.app")

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

	def test_app_module_count_under_budget(self):
		count = self.app_probe["modules"]
		self.assertLessEqual(
			count,
			APP_MODULE_BUDGET,
			f"`import frappe.app` loads {count} modules, over budget {APP_MODULE_BUDGET} — "
			"a heavy import crept onto the boot-preload path.",
		)

	def test_app_strict_denylist_absent(self):
		loaded = set(self.app_probe["loaded"])
		leaked = sorted(m for m in STRICT_DENYLIST if m in loaded)
		self.assertEqual(leaked, [], f"unexpected eager imports on frappe.app path: {leaked}")
