# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json
import os
import tempfile
from unittest.mock import patch

import frappe
from frappe.config import get_common_conf, patch_common_conf
from frappe.tests import IntegrationTestCase
from frappe.utils.modules import get_modules_from_all_apps_for_user


class TestConfig(IntegrationTestCase):
	def test_get_modules(self):
		frappe_modules = frappe.get_all("Module Def", filters={"app_name": "frappe"}, pluck="name")
		all_modules_data = get_modules_from_all_apps_for_user()
		all_modules = [x["module_name"] for x in all_modules_data]
		self.assertIsInstance(all_modules_data, list)
		self.assertFalse([x for x in frappe_modules if x not in all_modules])


class TestGetCommonConf(IntegrationTestCase):
	"""Phase 19: architecture knobs read ONLY from common_site_config.json."""

	def _sites_path(self, config: dict | None):
		tmp = tempfile.mkdtemp()
		if config is not None:
			with open(os.path.join(tmp, "common_site_config.json"), "w") as f:
				f.write(config if isinstance(config, str) else json.dumps(config))
		return tmp

	def test_reads_common_value(self):
		path = self._sites_path({"queue_backend": "rq"})
		with patch.object(frappe.local, "sites_path", path):
			self.assertEqual(get_common_conf("queue_backend"), "rq")

	def test_missing_file_returns_default(self):
		path = self._sites_path(None)
		with patch.object(frappe.local, "sites_path", path):
			self.assertIsNone(get_common_conf("queue_backend"))
			self.assertEqual(get_common_conf("in_process_scheduler", 1), 1)

	def test_malformed_json_raises(self):
		path = self._sites_path("{not json")
		with patch.object(frappe.local, "sites_path", path):
			self.assertRaises(Exception, get_common_conf, "queue_backend")

	def test_never_reads_site_config(self):
		# a site-level value must not leak into the common reader
		frappe.conf["use_node_realtime"] = 1
		self.addCleanup(frappe.conf.pop, "use_node_realtime", None)
		path = self._sites_path({})
		with patch.object(frappe.local, "sites_path", path):
			self.assertIsNone(get_common_conf("use_node_realtime"))

	def test_no_site_context(self):
		# works before/without frappe.init — falls back to cwd-relative path
		from frappe.config import clear_site_config_cache

		path = self._sites_path({"db_pool_size": 7})
		cwd = os.getcwd()
		os.chdir(path)
		self.addCleanup(os.chdir, cwd)
		# "." is the cache key — clear so it can't serve another dir's entry
		clear_site_config_cache()
		self.addCleanup(clear_site_config_cache)
		with patch.object(frappe.local, "sites_path", "."):
			self.assertEqual(get_common_conf("db_pool_size"), 7)

	def test_patch_common_conf(self):
		with patch_common_conf(queue_backend="arq"):
			self.assertEqual(get_common_conf("queue_backend"), "arq")
			with patch_common_conf(queue_backend=None):
				self.assertIsNone(get_common_conf("queue_backend"))
			self.assertEqual(get_common_conf("queue_backend"), "arq")
		# restored to the real common config afterwards
		self.assertEqual(get_common_conf("queue_backend"), frappe.get_common_site_config().get("queue_backend"))
