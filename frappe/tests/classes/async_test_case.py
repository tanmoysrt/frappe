import unittest

from .integration_test_case import IntegrationTestCase
from .unit_test_case import UnitTestCase

# NOTE: plain unittest.TestCase silently "passes" `async def` tests without
# awaiting them (the coroutine is created, never run — only a RuntimeWarning).
# These base classes exist so async tests actually execute.
#
# IsolatedAsyncioTestCase MUST come first in the bases: its async-aware test
# invocation wins the MRO, while UnitTestCase/IntegrationTestCase stay in the
# MRO so frappe's setUpClass utilities run and the test discovery
# (frappe/testing/discovery.py) categorizes these as "unit"/"integration".
#
# Caveat: IsolatedAsyncioTestCase spins up a fresh event loop per test method;
# loop state cannot be shared across test methods. Sync `def test_*` methods
# in the same class still work.


class AsyncUnitTestCase(unittest.IsolatedAsyncioTestCase, UnitTestCase):
	"""Unit test base class that runs `async def` test methods."""


class AsyncIntegrationTestCase(unittest.IsolatedAsyncioTestCase, IntegrationTestCase):
	"""Integration test base class that runs `async def` test methods."""
