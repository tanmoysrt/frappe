"""Phase 24.1: config-driven backend preload.

Every pluggable backend driver (rq, redis, the DB drivers) is lazy-imported by
default (see frappe.__init__ __getattr__ + the function-level imports in
monitor/realtime/global_search/background_jobs/database). At ASGI lifespan
startup this reads common_site_config and eager-imports ONLY the drivers the
configured backends actually use, so they are warm — and, once 24.7 lands,
gc.freeze-able — on the first request instead of import-locked on the hot path.
A driver that will never be used in this config is never imported and costs zero
pages. Universal: config decides what's warm, everything else stays cold.

No new env knobs — everything is read via frappe.get_common_conf.
"""

import importlib

import frappe


def preload_configured_backends() -> list[str]:
	"""Import the drivers the configured backends need; return what was warmed.
	Each import is best-effort: a missing optional driver is skipped, not fatal."""
	conf = frappe.get_common_conf
	warmed: list[str] = []

	def warm(*modules: str) -> None:
		for module in modules:
			if module in warmed:
				continue
			try:
				importlib.import_module(module)
				warmed.append(module)
			except ImportError:
				pass

	# cache backend
	if conf("cache_backend") == "redis":
		warm("redis")

	# queue backend (rq pulls redis); the disable_rq off-switch wins
	if conf("queue_backend") == "rq" and not conf("disable_rq"):
		warm("rq", "redis")

	# DB driver: only the one this bench's db_type + async flag selects
	db_type = conf("db_type")
	use_async = conf("use_async_db")
	if db_type == "mariadb":
		if use_async:
			warm("aiomysql", "pymysql")
		elif conf("use_mysqlclient"):
			warm("MySQLdb")
		else:
			warm("pymysql")
	elif db_type == "postgres":
		warm("psycopg" if use_async else "psycopg2")
	elif db_type == "sqlite":
		warm("libsql")
		if conf("queue_backend") == "sqlite":
			warm("aiosqlite")

	return warmed
