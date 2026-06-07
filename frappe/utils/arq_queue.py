# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""ARQ queue backend (Phase 10) — third opt-in backend, asyncio-native scale-out.

For deployments that outgrow the in-process SQLite queue but want asyncio
workers instead of RQ's forking model: multi-server, high throughput, arq's
cron/dashboard ecosystem. Opt in per site with ``queue_backend: "arq"``.

Producers stay sync (``frappe.enqueue`` unchanged) — the arq pool lives on
the process-wide bridge loop, calls go through ``run_coroutine_sync``. All
frappe queues share one arq stream (``frappe:queue``); arq's own job-id
dedup gives the same ``deduplicate`` contract as the other backends.

Workers run out-of-process::

    cd sites && ../env/bin/arq frappe.utils.arq_queue.WorkerSettings

Redis: ``arq_redis`` (or ``redis_queue``) from site/common config. Note:
arq 0.28 pins ``redis<6`` while frappe ships redis 7.x — the pin is
conservative, the round-trip works on redis-py 7.1 (smoke-tested); arq is
env-only until the Phase 18 dependency cleanup.
"""

import os

from asgiref.sync import sync_to_async

import frappe
from frappe.dispatch import run_coroutine_sync

ARQ_QUEUE_NAME = "frappe:queue"
MAX_TRIES = 3

_pool = {"pool": None, "pid": None}


def _redis_settings():
	from arq.connections import RedisSettings

	conf = frappe.get_conf()
	dsn = conf.get("arq_redis") or conf.get("redis_queue") or "redis://127.0.0.1:11001"
	return RedisSettings.from_dsn(dsn)


def _get_pool():
	"""Producer-side arq pool, bound to the bridge loop. Fork-safe via pid
	check; created in a clean contextvars Context (Phase 6 lesson — the
	pool's connections must not pin the creating request's frappe.local)."""
	if _pool["pool"] is None or _pool["pid"] != os.getpid():
		from arq.connections import create_pool

		from frappe.database.aio import run_in_clean_context

		settings = _redis_settings()  # read config caller-side, pass plain values
		_pool["pool"] = run_coroutine_sync(run_in_clean_context(create_pool(settings)))
		_pool["pid"] = os.getpid()
	return _pool["pool"]


def enqueue_job(queue_args: dict, queue: str, job_id: str, max_retries: int = MAX_TRIES):
	"""Same producer contract as the sqlite backend: returns the job, or None
	when ``job_id`` is already enqueued (arq's built-in job-id dedup)."""
	pool = _get_pool()
	return run_coroutine_sync(
		pool.enqueue_job("run_frappe_job", queue_args, _job_id=job_id, _queue_name=ARQ_QUEUE_NAME)
	)


def is_job_enqueued(job_id: str) -> bool:
	from arq.jobs import Job, JobStatus

	status = run_coroutine_sync(Job(job_id, _get_pool(), _queue_name=ARQ_QUEUE_NAME).status())
	return status in (JobStatus.deferred, JobStatus.queued, JobStatus.in_progress)


async def run_frappe_job(ctx, queue_args: dict):
	"""Worker-side task: same shape as the sqlite queue worker — execute_job
	(hooks/retry/transaction semantics) on the thread pool, in a fresh
	contextvars Context per job; method resolution and async-def bridging
	happen inside the job's own site context (sqlite_queue._make_runner)."""
	from frappe.database.aio import run_in_clean_context
	from frappe.utils.background_jobs import execute_job
	from frappe.utils.sqlite_queue import _make_runner

	method = queue_args["method"]
	func_name = method if isinstance(method, str) else f"{method.__module__}.{method.__qualname__}"
	await run_in_clean_context(
		sync_to_async(execute_job, thread_sensitive=False)(
			site=queue_args["site"],
			method=_make_runner(method, func_name),
			event=queue_args.get("event"),
			job_name=queue_args.get("job_name"),
			kwargs=queue_args.get("kwargs") or {},
			user=queue_args.get("user"),
			is_async=True,
		)
	)


class WorkerSettings:
	"""``arq frappe.utils.arq_queue.WorkerSettings`` (run from the sites dir
	so config resolution finds common_site_config.json)."""

	functions = (run_frappe_job,)
	queue_name = ARQ_QUEUE_NAME
	max_tries = MAX_TRIES
	redis_settings = None  # resolved in __init_subclass__-free way below


# arq reads class attributes; resolve the dsn when this module is imported
# by the arq CLI (cwd=sites). Guarded: an import from outside a bench (no
# resolvable config) must not explode — arq then falls back to its default.
try:
	WorkerSettings.redis_settings = _redis_settings()
except Exception:
	pass
