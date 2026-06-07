"""Phase 24: live memory + cache observability.

`memstats` is a System-Manager-only whitelisted method (RPC path
`frappe.utils.memstats.memstats`). scripts/memprofile.py 'stats' mode calls the
same function so the script and the desk report identical numbers. Kept in its
own module (not utils/__init__) so the `@frappe.whitelist()` decorator runs
after frappe has finished initialising — utils/__init__ loads too early.
"""

import gc
import os
import sys

import frappe


@frappe.whitelist()
def memstats(top: int = 0) -> dict:
	"""RSS/Pss (smaps_rollup), module count, gc + freeze stats, controller and
	cache sizes — observability for long-running idle drift. ``top`` > 0 returns
	the tracemalloc top-N allocations, but ONLY if the caller already started
	tracemalloc (~2x slowdown, never default). Every section is guarded so an
	unknown cache backend never raises."""
	frappe.only_for("System Manager")

	top = int(top)
	pid = os.getpid()
	stats: dict = {
		"pid": pid,
		"modules": len(sys.modules),
		"gc_objects": len(gc.get_objects()),
		"gc_counts": list(gc.get_count()),
	}
	try:
		stats["gc_frozen"] = gc.get_freeze_count()
	except Exception:
		pass

	try:
		with open(f"/proc/{pid}/smaps_rollup") as fh:
			for line in fh:
				for field in ("Rss:", "Pss:", "Anonymous:"):
					if line.startswith(field):
						name = field.rstrip(":").lower() + "_mb"
						stats[name] = round(int(line.split()[1]) / 1024, 1)
	except OSError:
		pass

	stats["controllers"] = len(getattr(frappe, "controllers", ()) or ())
	stats["lazy_controllers"] = len(getattr(frappe, "lazy_controllers", ()) or ())

	backend = getattr(frappe, "cache", None)
	if backend is not None:
		cache_stats = {"type": type(backend).__name__}
		# InProcessCache exposes raw maps + hit/miss counters
		for attr, label in (
			("_strings", "strings"),
			("_hashes", "hashes"),
			("_sets", "sets"),
			("_lists", "lists"),
		):
			store = getattr(backend, attr, None)
			if store is not None:
				cache_stats[label] = len(store)
		for counter in ("hits", "misses"):
			if hasattr(backend, counter):
				cache_stats[counter] = getattr(backend, counter)
		stats["cache"] = cache_stats

	client_cache = getattr(frappe, "client_cache", None)
	if client_cache is not None:
		cc = {"type": type(client_cache).__name__}
		local = getattr(client_cache, "cache", None)
		if local is not None:
			cc["entries"] = len(local)
		for attr in ("maxsize", "hits", "misses"):
			if hasattr(client_cache, attr):
				cc[attr] = getattr(client_cache, attr)
		stats["client_cache"] = cc

	# Phase 25.1: libsql decltype-map rebuild counter (only meaningful on a
	# sqlite/libsql site). A healthy process plateaus; a climbing count means
	# real columns keep appearing unknown (DDL churn or a gate bug).
	libsql = sys.modules.get("frappe.database.sqlite.libsql_compat")
	if libsql is not None:
		stats["libsql_decltype_rebuilds"] = getattr(libsql, "_decltype_rebuilds", None)

	if top > 0:
		import tracemalloc

		if tracemalloc.is_tracing():
			snapshot = tracemalloc.take_snapshot()
			stats["tracemalloc_top"] = [
				{
					"trace": str(s.traceback),
					"size_kb": round(s.size / 1024, 1),
					"count": s.count,
				}
				for s in snapshot.statistics("lineno")[:top]
			]
		else:
			stats["tracemalloc_top"] = "not tracing (start tracemalloc before calling)"

	return stats
