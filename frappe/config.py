import importlib
import os
import traceback
from contextlib import contextmanager
from typing import Any

import click

import frappe
from frappe import _dict, get_file_json
from frappe.exceptions import IncorrectSitePath
from frappe.utils.caching import site_cache

# Architecture-level options (Phase 19): these choose the process shape
# (async DB driver, queue backend, realtime mode, serve knobs) — a bench
# runs ONE process shape, so they live ONLY in common_site_config.json and
# are never site-overridable. Read them with `frappe.get_common_conf`.
ARCHITECTURE_KEYS = (
	"use_async_db",
	"queue_backend",
	"use_node_realtime",
	"socketio_port",
	"db_pool_size",
	"db_pool_recycle",
	"db_pool_idle_timeout",
	"db_pool_acquire_timeout",
	"in_process_scheduler",
	"cache_backend",
	"asgi_pool_size",
	"asgi_limit_concurrency",
	"asgi_thread_stack",
	"malloc_trim_interval",
	"use_tcmalloc",
	"webserver_port",
	"free_threading",
)

# pinned by patch_common_conf during tests; checked first by get_common_conf
_common_conf_overrides: dict[str, Any] | None = None


def get_site_config(
	sites_path: str | None = None,
	site_path: str | None = None,
	*,
	cached=False,
) -> _dict[str, Any]:
	"""Return `site_config.json` combined with `sites/common_site_config.json`.
	`site_config` is a set of site wide settings like database name, password, email etc.
	"""

	sites_path = sites_path or getattr(frappe.local, "sites_path", ".")
	site_path = site_path or getattr(frappe.local, "site_path", None)

	if cached:
		return _cached_get_site_config(sites_path, site_path).copy()
	else:
		return _get_site_config(sites_path, site_path)


def _get_site_config(sites_path: str, site_path: str) -> _dict[str, Any]:
	config: _dict[str, Any] = _dict()

	common_config = get_common_site_config(sites_path)

	if sites_path:
		config.update(common_config)

	if site_path:
		site_config = os.path.join(site_path, "site_config.json")
		if os.path.exists(site_config):
			try:
				site_overrides = get_file_json(site_config)
				config.update(site_overrides)
				_warn_site_architecture_keys(site_path, site_overrides)
			except Exception as error:
				click.secho(f"{frappe.local.site}/site_config.json is invalid", fg="red")
				print(error)
				raise
		elif frappe.local.site and not frappe.local.flags.new_site:
			error_msg = f"{frappe.local.site} does not exist."
			if common_config.developer_mode:
				from frappe.utils import get_sites

				all_sites = get_sites()
				error_msg += "\n\nSites on this bench:\n"
				error_msg += "\n".join(f"* {site}" for site in all_sites)

			raise IncorrectSitePath(error_msg)

	# Generalized env variable overrides and defaults
	def db_default_ports(db_type):
		if db_type == "mariadb":
			from frappe.database.mariadb.database import MariaDBDatabase

			return MariaDBDatabase.default_port
		elif db_type == "postgres":
			from frappe.database.postgres.database import PostgresDatabase

			return PostgresDatabase.default_port

		raise ValueError(f"Unsupported db_type={db_type}")

	config["redis_queue"] = (
		os.environ.get("FRAPPE_REDIS_QUEUE") or config.get("redis_queue") or "redis://127.0.0.1:11311"
	)
	config["redis_cache"] = (
		os.environ.get("FRAPPE_REDIS_CACHE") or config.get("redis_cache") or "redis://127.0.0.1:13311"
	)
	config["db_type"] = os.environ.get("FRAPPE_DB_TYPE") or config.get("db_type") or "mariadb"

	if config["db_type"] in ("mariadb", "postgres"):
		config["db_socket"] = os.environ.get("FRAPPE_DB_SOCKET") or config.get("db_socket")
		config["db_host"] = os.environ.get("FRAPPE_DB_HOST") or config.get("db_host") or "127.0.0.1"
		config["db_port"] = int(
			os.environ.get("FRAPPE_DB_PORT") or config.get("db_port") or db_default_ports(config["db_type"])
		)

		# Set the user as database name if not set in config
		config["db_user"] = os.environ.get("FRAPPE_DB_USER") or config.get("db_user") or config.get("db_name")

		# read password
		config["db_password"] = os.environ.get("FRAPPE_DB_PASSWORD") or config.get("db_password")

	# vice versa for dbname if not defined
	config["db_name"] = os.environ.get("FRAPPE_DB_NAME") or config.get("db_name") or config["db_user"]

	# Allow externally extending the config with hooks
	if extra_config := config.get("extra_config"):
		if isinstance(extra_config, str):
			extra_config = [extra_config]
		for hook in extra_config:
			try:
				module, method = hook.rsplit(".", 1)
				config |= getattr(importlib.import_module(module), method)()
			except Exception:
				print(f"Config hook {hook} failed")
				traceback.print_exc()

	return config


_warned_arch_key_sites: set[str] = set()


def _warn_site_architecture_keys(site_path: str, site_overrides: dict) -> None:
	"""Architecture keys in a site_config.json are IGNORED since Phase 19
	(their readers use `get_common_conf`); warn once per site per process."""
	stale = [key for key in ARCHITECTURE_KEYS if key in site_overrides]
	if stale and site_path not in _warned_arch_key_sites:
		_warned_arch_key_sites.add(site_path)
		click.secho(
			f"Warning: architecture keys {stale} in {site_path}/site_config.json are "
			"ignored — set them in common_site_config.json instead",
			fg="yellow",
			err=True,
		)


def get_common_site_config(sites_path: str | None = None, cached=False) -> _dict[str, Any]:
	"""Return common site config as dictionary.

	This is useful for:
	- checking configuration which should only be allowed in common site config
	- When no site context is present and fallback is required.
	"""
	sites_path = sites_path or getattr(frappe.local, "sites_path", ".")
	if cached:
		return _cached_get_common_site_config(sites_path).copy()
	else:
		return _get_common_site_config(sites_path)


def _get_common_site_config(sites_path: str) -> _dict[str, Any]:
	common_site_config = os.path.join(sites_path, "common_site_config.json")
	if os.path.exists(common_site_config):
		try:
			return _dict(get_file_json(common_site_config))
		except Exception as error:
			click.secho("common_site_config.json is invalid", fg="red")
			print(error)
			raise
	return _dict()


# These variants cache the values in *memory* for repeat access, use it in web requests or anywhere
# else it helps to avoid recurring accesses in *long-lived* processes.
_cached_get_site_config = site_cache(ttl=60, maxsize=16)(_get_site_config)
_cached_get_common_site_config = site_cache(ttl=60, maxsize=16)(_get_common_site_config)


def clear_site_config_cache():
	_cached_get_common_site_config.clear_cache()
	_cached_get_site_config.clear_cache()


def get_common_conf(key: str, default: Any = None) -> Any:
	"""Read an architecture-level option from common_site_config.json ONLY.

	Unlike ``frappe.conf`` (common merged with site config, site winning),
	this never consults the site config — architecture knobs (see
	``ARCHITECTURE_KEYS``) choose the process shape and must not vary per
	site. Works without site init (uses cwd as sites_path fallback) and is
	cached per process (60s TTL via the cached common-config reader).
	"""
	if _common_conf_overrides is not None and key in _common_conf_overrides:
		return _common_conf_overrides[key]
	value = get_common_site_config(cached=True).get(key)
	return default if value is None else value


@contextmanager
def patch_common_conf(**overrides):
	"""Pin architecture knobs for a test block without touching the
	bench-global common_site_config.json (mirror of `change_settings`).

	with patch_common_conf(queue_backend="rq"): ...
	"""
	global _common_conf_overrides
	previous = _common_conf_overrides
	_common_conf_overrides = {**(previous or {}), **overrides}
	try:
		yield
	finally:
		_common_conf_overrides = previous


def get_conf(site: str | None = None) -> _dict[str, Any]:
	if hasattr(frappe.local, "conf"):
		return frappe.local.conf

	# if no site, get from common_site_config.json
	with frappe.init_site(site):
		return frappe.local.conf
