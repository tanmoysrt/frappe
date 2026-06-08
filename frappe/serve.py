# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Light-mode server: entrypoint + runtime, in one place inside the framework.

`python -m frappe.serve` is the production light-mode launcher (Procfile `web`).
This module merges what used to be three things:
  - the bench-root `app.py` malloc/GIL re-exec shim,
  - `frappe.asgi.serve()` (the uvicorn runtime),
  - the `bench serve` wrapper in `frappe.app.serve`.

Two layers, kept apart on purpose:

1. `_reexec()` — pure stdlib, runs BEFORE any heavy `import frappe.*`.
   MALLOC_ARENA_MAX / LD_PRELOAD (tcmalloc) / PYTHON_GIL only take effect at
   process start, so when the running interpreter's env doesn't already match
   what the site wants, we `os.execve` once into a corrected process. When it
   already matches (e.g. the Procfile pre-sets MALLOC_ARENA_MAX), no re-exec
   happens and frappe is imported exactly once.

2. `serve()` — the uvicorn runtime (sized thread pool, idle malloc_trim +
   post-warmup gc.freeze, lifespan). Also the `bench serve` target, where no
   re-exec is wanted (dev), so it is callable on its own.

Config: sites/common_site_config.json only (no env-var knobs). Runtime keys:
asgi_pool_size, asgi_limit_concurrency, webserver_port, asgi_thread_stack,
malloc_trim_interval, loop_debug, log_level.
"""

import asyncio
import json
import os
import sys

_TCMALLOC = "/usr/lib64/libtcmalloc_minimal.so.4"
# .../bench/apps/frappe/frappe/serve.py -> up 4 = bench root
_BENCH_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_SITES_PATH = os.environ.get("SITES_PATH") or os.path.join(_BENCH_ROOT, "sites")


def _read_common_config():
	path = os.path.join(_SITES_PATH, "common_site_config.json")
	try:
		with open(path, encoding="utf-8") as f:
			return json.load(f)
	except FileNotFoundError:
		return {}


def _flag(conf, key, default=0):
	return str(conf.get(key, default)) in ("1", "True", "true")


def _lean_backends(conf):
	"""Smallest backend set: sqlite main DB + in-process cache + sqlite queue.
	No external I/O — so the lean default (glibc allocator) applies."""
	return (
		conf.get("db_type") == "sqlite"
		and conf.get("cache_backend") in (None, "", "memory")
		and conf.get("queue_backend") in (None, "", "sqlite")
	)


def _is_free_threaded_build():
	"""True on a no-GIL CPython build (PEP 703), regardless of runtime toggle."""
	import sysconfig

	return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def _gil_env_decision(conf):
	"""Phase 23: free-threading is opt-in and only worthwhile with the async
	stack (the thread pool runs truly parallel only when DB/IO don't block it).

	Returns the PYTHON_GIL value to force, or None to leave as-is:
	- non free-threaded build → None (knob is a no-op, GIL always on)
	- free-threaded build + `free_threading` + `use_async_db` → "0" (GIL off)
	- free-threaded build otherwise → "1" (GIL on; "use gil with async only")
	"""
	if not _is_free_threaded_build():
		return None
	# use_async_db defaults on (Phase 28); free_threading is the opt-in here
	return "0" if (_flag(conf, "free_threading") and _flag(conf, "use_async_db", 1)) else "1"


def _reexec():
	"""Re-exec into a process whose allocator/GIL env matches the site, but ONLY
	if the current process doesn't already match — so the common path (Procfile
	pre-sets MALLOC_ARENA_MAX, lean site, GIL build) imports frappe just once.

	glibc spawns one malloc arena per thread by default; the thread pool would
	multiply arenas and fragmentation, and this process runs for weeks (no
	gunicorn max_requests recycling), so MALLOC_ARENA_MAX caps that. Lean sites
	default to glibc — the idle malloc_trim tick returns freed pages, which
	tcmalloc ignores (Phase 25.2); heavy backends default to tcmalloc.
	use_tcmalloc / free_threading in common config override."""
	if os.environ.get("_FRAPPE_MALLOC_READY"):
		return
	conf = _read_common_config()
	env = dict(os.environ)
	changed = False
	if "MALLOC_ARENA_MAX" not in env:
		env["MALLOC_ARENA_MAX"] = "2"
		changed = True
	if _flag(conf, "use_tcmalloc", 0 if _lean_backends(conf) else 1) and os.path.exists(_TCMALLOC):
		if _TCMALLOC not in env.get("LD_PRELOAD", ""):
			preload = env.get("LD_PRELOAD", "")
			env["LD_PRELOAD"] = _TCMALLOC + (":" + preload if preload else "")
			changed = True
	gil = _gil_env_decision(conf)
	if gil is not None and env.get("PYTHON_GIL") != gil:
		env["PYTHON_GIL"] = gil
		changed = True
	if not changed:
		# env already correct (e.g. set in the Procfile) — no execve, single import
		os.environ["_FRAPPE_MALLOC_READY"] = "1"
		return
	env["_FRAPPE_MALLOC_READY"] = "1"
	# Re-exec via `-m frappe.serve`, NOT the script path: under `-m`, sys.argv[0]
	# is this file's path, so execve([python, *sys.argv]) would run serve.py by
	# path — putting the package dir (frappe/) on sys.path[0], where frappe's own
	# locale.py / email/ shadow the stdlib. `-m` keeps the import root correct.
	os.execve(sys.executable, [sys.executable, "-m", "frappe.serve"], env)


def serve(port=None, site=None, sites_path=".", proxy=False):
	"""Programmatic uvicorn server — the light-mode runtime and the `bench serve`
	target. No re-exec here (that's `_reexec`, run by `main()` before frappe is
	imported); `bench serve` reaches this directly for dev.

	Knobs — sites/common_site_config.json only (no env vars): asgi_pool_size,
	asgi_limit_concurrency, webserver_port, asgi_thread_stack,
	malloc_trim_interval, loop_debug, log_level.
	"""
	import ctypes
	import gc
	import threading
	from concurrent.futures import ThreadPoolExecutor

	import uvicorn

	import frappe
	import frappe.app
	import frappe.dispatch

	if site:
		frappe.app._site = site
	frappe.app._sites_path = sites_path
	os.environ["SITES_PATH"] = sites_path
	if proxy:
		os.environ["USE_PROXY"] = "1"

	conf = frappe.get_common_site_config(sites_path)

	def knob(key, default):
		value = conf.get(key)
		return int(value) if value is not None else default

	cpu = os.cpu_count() or 1
	is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
	gil_disabled = is_gil_enabled is not None and not is_gil_enabled()
	# GIL build: 2 x CPU overlaps I/O waits. Free-threaded (Phase 23): threads
	# run truly parallel, so ~CPU-sized pool avoids CPU oversubscription.
	default_pool = cpu if gil_disabled else 2 * cpu
	# Phase 24.10 lean default: the smallest backend set (sqlite main DB +
	# in-process cache + sqlite queue) has no external I/O to overlap, so a
	# CPU-sized pool is just wasted thread stacks — cap it small. Same predicate
	# _reexec uses to pick the glibc allocator, so the two can't drift.
	if _lean_backends(conf):
		default_pool = min(default_pool, 4)

	pool_size = knob("asgi_pool_size", default_pool)
	# Fixed default, not CPU-derived: 16 in-flight requests is plenty for a
	# single-process light-mode site; raise asgi_limit_concurrency for heavier
	# deployments. (The pool still bounds how many run at once; excess queue.)
	limit_concurrency = knob("asgi_limit_concurrency", 16)
	port = int(port) if port else knob("webserver_port", 8001)
	thread_stack = knob("asgi_thread_stack", 512 * 1024)
	trim_interval = knob("malloc_trim_interval", 30)

	async def _idle_trim():
		"""Replaces gunicorn max_requests recycling: release freed pages back to
		the OS so heap fragmentation doesn't accumulate in a process that runs
		for weeks. malloc_trim(0) is a glibc API, a no-op under tcmalloc."""
		try:
			libc = ctypes.CDLL("libc.so.6")
		except OSError:
			libc = None
		warmed_up = False
		while True:
			await asyncio.sleep(trim_interval)
			# gc.collect() finalizers must run on the bridge-loop thread: a bare
			# gc.collect() here (this is the main uvicorn loop thread) would tear
			# down aiomysql transports cross-thread and wedge all DB I/O.
			await frappe.dispatch.gc_collect_safe()
			# Phase 24.7: one-shot freeze after the first warmup cycle — by now
			# the initial requests have populated meta/controllers; move that
			# now-stable graph out of GC scanning too (lifespan startup already
			# froze the import graph + preloaded drivers). Cumulative, no-GIL safe.
			if not warmed_up:
				gc.freeze()
				warmed_up = True
			if libc is not None:
				try:
					libc.malloc_trim(0)
				except Exception:
					pass

	async def _main():
		loop = asyncio.get_running_loop()
		# smaller stacks: default 8 MB per pool thread is pure waste here
		threading.stack_size(thread_stack)
		loop.set_default_executor(
			ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="frappe_sync")
		)
		threading.stack_size(0)
		gil_status = "unknown" if is_gil_enabled is None else ("disabled" if gil_disabled else "enabled")
		print(
			f"frappe ASGI boot: pool={pool_size}, limit_concurrency={limit_concurrency}, "
			f"port={port}, gil={gil_status}",
			file=sys.stderr,
		)
		if conf.get("loop_debug"):
			# dev: with one loop a blocking call stalls the whole process — log
			# any callback/step that holds it too long
			loop.set_debug(True)
			loop.slow_callback_duration = 0.1
		trim_task = asyncio.ensure_future(_idle_trim())
		config = uvicorn.Config(
			"frappe.asgi:application",
			host="0.0.0.0",
			port=port,
			loop="asyncio",
			interface="asgi3",
			lifespan="on",
			limit_concurrency=limit_concurrency,
			log_level=conf.get("log_level", "info"),
			access_log=False,
		)
		try:
			await uvicorn.Server(config).serve()
		finally:
			trim_task.cancel()

	asyncio.run(_main())


def main():
	"""Light-mode entrypoint: correct the allocator/GIL env (re-exec if needed)
	BEFORE importing frappe, then serve from the bench's sites dir."""
	_reexec()
	os.environ["SITES_PATH"] = _SITES_PATH
	os.chdir(_SITES_PATH)
	serve(sites_path=".")


if __name__ == "__main__":
	main()
