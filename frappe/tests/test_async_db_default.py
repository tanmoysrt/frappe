"""Phase 28: the async DB driver is the default.

`async_db_enabled()` is the single source of the `use_async_db` default; get_db's
backend branches and the lifespan preload both read it, so they can't drift. This
pins the default-on contract and the explicit rollback.
"""

from unittest.mock import patch

import frappe
from frappe.config import patch_common_conf
from frappe.database import async_db_enabled
from frappe.tests import UnitTestCase


class TestAsyncDBDefault(UnitTestCase):
	def test_defaults_on_when_unset(self):
		# the flag absent -> helper must pass default 1 to get_common_conf.
		# Patch the reader so this holds regardless of the bench's own config.
		with patch.object(frappe, "get_common_conf", lambda key, default=None: default):
			self.assertTrue(async_db_enabled())

	def test_explicit_off_is_respected(self):
		with patch_common_conf(use_async_db=0):
			self.assertFalse(async_db_enabled())

	def test_explicit_on(self):
		with patch_common_conf(use_async_db=1):
			self.assertTrue(async_db_enabled())
