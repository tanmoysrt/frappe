"""This file houses all Frappe specific optimizations and hooks that run on startup or during fork.

Warning: This entire file is private as indicated by `_` prefix in filename.
"""

import faulthandler
import gc
import glob
import io
import os
import re
import signal
import sys
from functools import lru_cache
from pathlib import Path

import psutil


def optimize_all():
	"""Single entry point to enable all optimizations at right time automatically."""

	# Note:
	# - This function is ALWAYS executed as soon as `import frappe` ends.
	# - Any deferred work should be deferred using os module's fork hooks.
	# - Respect configurations using environement variables.
	# - fork hooks can not be unregistered, so care should be taken to execute them only when they
	#   make sense.
	optimize_regex_cache()
	optimize_gc_parameters()
	optimize_gc_for_copy_on_write()
	optimize_for_gil_contention()


def optimize_werkzeug_import():
	"""Avoid executing werkzeug/__init__.py when only submodules are needed.

	werkzeug's __init__ eagerly imports `serving`, `test` and `wrappers`, which in turn
	pull in heavy stdlib modules (ssl, email, socketserver, ...) costing ~2 MB per
	process even when only `werkzeug.datastructures` is required (e.g. background
	workers that never serve HTTP).

	This pre-seeds `sys.modules["werkzeug"]` with the package module *without*
	executing its __init__. Submodule imports (`from werkzeug.wrappers import ...`)
	work as usual via the package's __path__. Top-level attribute access
	(`from werkzeug import Response`) is handled by a PEP 562 module __getattr__
	that lazily executes the real __init__ once, preserving full compatibility.

	Note: This function must run *before* anything imports werkzeug; it's a no-op
	otherwise.
	"""
	import importlib
	import importlib.util

	if "werkzeug" in sys.modules:
		return

	spec = importlib.util.find_spec("werkzeug")
	if spec is None or spec.loader is None:  # pragma: no cover
		return

	module = importlib.util.module_from_spec(spec)
	executed = False

	def __getattr__(name):
		nonlocal executed
		# `from werkzeug import exceptions`-style submodule access (also used by
		# werkzeug internally via `from .. import x`): import just the submodule.
		if not name.startswith("_"):
			try:
				return importlib.import_module(f"werkzeug.{name}")
			except ImportError:
				pass
		# A real top-level attribute (Request, Response, Client, run_simple):
		# execute the real werkzeug/__init__.py once.
		if not executed:
			executed = True
			spec.loader.exec_module(module)
		try:
			return module.__dict__[name]
		except KeyError:
			raise AttributeError(f"module 'werkzeug' has no attribute {name!r}") from None

	module.__getattr__ = __getattr__
	sys.modules["werkzeug"] = module


def optimize_gc_parameters():
	from frappe.utils import sbool

	if not bool(sbool(os.environ.get("FRAPPE_TUNE_GC", True))):
		return

	# generational GC gets triggered after certain allocs (g0) which is 700 by default.
	# This number is quite small for frappe where a single query can potentially create 700+
	# objects easily.
	# Bump this number higher, this will make GC less aggressive but that improves performance of
	# everything else.
	g0, g1, g2 = gc.get_threshold()  # defaults are 700, 10, 10.
	gc.set_threshold(g0 * 10, g1 * 2, g2 * 2)


def optimize_regex_cache():
	# Remove references to pattern that are pre-compiled and loaded to global scopes.
	# Leave that cache for dynamically generated regex.
	os.register_at_fork(before=re.purge)


def register_fault_handler():
	# Some libraries monkey patch stderr, we need actual fd
	if isinstance(sys.__stderr__, io.TextIOWrapper):
		faulthandler.enable()
		faulthandler.register(signal.SIGUSR1, file=sys.__stderr__)


def optimize_gc_for_copy_on_write():
	from frappe.utils import sbool

	if not bool(sbool(os.environ.get("FRAPPE_TUNE_GC", True))):
		return

	os.register_at_fork(before=freeze_gc)


_gc_frozen = False
worker_num = os.getpid()


def freeze_gc():
	global _gc_frozen
	if _gc_frozen:
		return
	# Both Gunicorn and RQ use forking to spawn workers. In an ideal world, the fork should be sharing
	# most of the memory if there are no writes made to data because of Copy on Write, however,
	# python's GC is not CoW friendly and writes to data even if user-code doesn't. Specifically, the
	# generational GC which stores and mutates every python object: `PyGC_Head`
	#
	# Calling gc.freeze() moves all the objects imported so far into permanant generation and hence
	# doesn't mutate `PyGC_Head`
	#
	# Refer to issue for more info: https://github.com/frappe/frappe/issues/18927
	gc.collect()
	gc.freeze()
	# RQ workers constantly fork, there' no benefit in doing this in that case.
	_gc_frozen = True


def optimize_for_gil_contention():
	if not os.environ.get("FRAPPE_PERF_PIN_WORKERS"):
		return

	if "gunicorn" not in str(sys.argv[0]):
		return

	if os.environ.get("FRAPPE_PERF_PIN_WORKERS_DETERMINISTIC"):
		# Ensure same pinning order every time.
		# This is only useful for benchmarking, DO NOT enable this in production.
		global worker_num
		worker_num = 0

	if not psutil.LINUX:
		# No need to support Mac, this optimization is only useful on _real_ servers.
		return

	# Populate the cache to avoid recomputing this in future.
	_ = parse_thread_siblings()
	os.register_at_fork(after_in_parent=increment_worker_count, after_in_child=pin_web_worker_to_one_core)


def increment_worker_count():
	# Not all forked workers will have incrementing numbers.
	# This psuedo-counter ensures a deterministic round-robbin style assignment of workers.
	global worker_num

	worker_num += 1


def assign_core(
	pid: int,
	physical_cpu_count: int,
	logical_cpu_count: int,
	current_affinity: list[int],
	thread_siblings: list[tuple[int, ...]],
) -> int | None:
	if len(current_affinity) == 1:  # Already set
		return current_affinity[0]

	if sorted(current_affinity) == list(range(physical_cpu_count)) and (
		physical_cpu_count == logical_cpu_count
	):
		# There is no SMT, we can just pick a real core in round-robbin fashion
		# This assumption can be wrong if some logical cores are disabled in weird manner, though if
		# you do that you probably know what you're doing.
		physical_core = current_affinity[pid % physical_cpu_count]
		return physical_core

	# If there is SMT then we need to be careful not to co-schedule. This can be a problem when you
	# have 2 workers but both are running on same physical core so expected parallelism is ~1.3x
	# instead of 2x.

	# This assignment is best understood with an example.
	# E.g. If there are 4 cores and 8 HW threads then two most common thread sibling patterns are:
	#       A: 0-1, 2-3, 4-5, 6-7
	#       B: 0-4, 1-5, 2-6, 3-7
	# The ideal rounding robbin assignment for both is 1 from each real core first and then assign
	# other threads. Which would translate to:
	# #:  0, 1, 2, 3, 4, 5, 6, 7
	# A: 0, 2, 4, 6, 1, 3, 5, 7 (repeated)
	# B: 0, 1, 2, 3, 4, 5, 6, 7 (repeated)

	if not thread_siblings:
		return
	thread_bucket = thread_siblings[pid % len(thread_siblings)]
	logical_core = thread_bucket[(pid % (len(thread_siblings) * len(thread_bucket))) // len(thread_siblings)]
	return logical_core


def pin_web_worker_to_one_core():
	"""Try to assign current process to one core."""

	core = assign_core(
		pid=worker_num,
		physical_cpu_count=psutil.cpu_count(logical=False),
		logical_cpu_count=psutil.cpu_count(logical=True),
		current_affinity=sorted(os.sched_getaffinity(0)),
		thread_siblings=parse_thread_siblings(),
	)
	if core is not None:
		psutil.Process().cpu_affinity([core])


@lru_cache(maxsize=1)
def parse_thread_siblings() -> list[tuple[int, int]] | None:
	try:
		threads_list = set()

		siblings_pattern = "/sys/devices/system/cpu/cpu[0-9]*/topology/core_cpus_list"
		for path in glob.glob(siblings_pattern):
			threads_list.add(tuple(int(cpu) for cpu in Path(path).read_text().replace("-", ",").split(",")))
		return sorted(threads_list)
	except Exception as e:
		import frappe

		logger = frappe.logger(with_more_info=True)
		logger.error(f"failed to parse thread siblings: {e}")
