# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""SQLite-backed task queue (Phase 9) — runs next to RQ, opt-in per site.

One bench-wide SQLite file (``sites/task_queue.db``) holds the ``jobs``
table. Producers (``frappe.enqueue`` with ``queue_backend: "sqlite"``) write
with plain sqlite3 from whatever thread they're on; consumers are 3-4
asyncio worker tasks living in the main event loop (started at ASGI lifespan
startup) — same process as HTTP, no worker processes, no queue Redis.

Jobs are claimed atomically with a single ``UPDATE ... RETURNING`` (SQLite
>= 3.35), so concurrent in-loop workers can never grab the same row. Sync
job functions run through ``execute_job`` (same hooks/retry/transaction
semantics as RQ workers) on the shared thread pool; ``async def`` job
functions are awaited on the main loop via the Phase-2 dispatch bridge.

Failed jobs retry up to ``max_retries`` (default 3) and then stay in the
table with their traceback. Successful jobs are deleted. A startup reaper
requeues ``running`` rows: the workers all live in this one process, so any
row still ``running`` at boot belonged to a dead process (e.g. the Phase-1
memory soft-restart killed it mid-job) and would otherwise be stuck forever.

Not supported (vs RQ): per-job timeout enforcement (an in-process job can't
be killed safely), ``at_front`` (FIFO by rowid only), success/failure
callbacks. Sites needing those keep ``queue_backend: "rq"``.
"""

import asyncio
import contextlib
import os
import pickle
import sqlite3
import threading

from asgiref.sync import sync_to_async

import frappe
from frappe.dispatch import dispatch_sync

DEFAULT_WORKERS = 3
POLL_INTERVAL = 2.0  # event-less fallback: picks up rows written by other processes (bench CLI)
MAX_RETRIES = 3

# same memory hygiene as the Phase 5 DB connections: don't inherit defaults
PRAGMAS = {
	"journal_mode": "WAL",
	"synchronous": "NORMAL",
	"busy_timeout": 5000,
	"cache_size": -2048,  # 2 MiB page cache
	"mmap_size": 0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	job_id TEXT UNIQUE,
	site TEXT NOT NULL,
	queue TEXT NOT NULL DEFAULT 'default',
	func TEXT NOT NULL,
	kwargs BLOB NOT NULL,
	status TEXT NOT NULL DEFAULT 'pending',
	retries INTEGER NOT NULL DEFAULT 0,
	max_retries INTEGER NOT NULL DEFAULT 3,
	error TEXT,
	enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
	started_at TEXT,
	ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_queue_status ON jobs(queue, status);
"""

# worker-side state, all bound to the loop start_workers() ran on
_state = {"loop": None, "tasks": [], "event": None, "conn": None, "pid": None}
_state_lock = threading.Lock()


def get_queue_db_path() -> str:
	sites_path = os.environ.get("SITES_PATH") or getattr(frappe.local, "sites_path", None) or "."
	return os.path.abspath(os.path.join(sites_path, "task_queue.db"))


def _apply_pragmas_sql():
	return "".join(f"PRAGMA {k}={v};" for k, v in PRAGMAS.items())


def _connect() -> sqlite3.Connection:
	"""Short-lived producer connection (one INSERT/SELECT, autocommit)."""
	conn = sqlite3.connect(get_queue_db_path(), timeout=5, isolation_level=None)
	conn.executescript(_apply_pragmas_sql() + SCHEMA)
	conn.row_factory = sqlite3.Row
	return conn


class SQLiteJob:
	"""Minimal stand-in for the RQ Job callers see (``job.id``)."""

	def __init__(self, job_id):
		self.id = job_id


def enqueue_job(queue_args: dict, queue: str, job_id: str, max_retries: int = MAX_RETRIES):
	"""Insert one job row. ``queue_args`` is the same dict the RQ path builds
	(site/user/method/event/job_name/is_async/kwargs) — pickled whole, like RQ.

	``job_id`` is the site-namespaced dedup id (UNIQUE): if a row with the
	same id is already pending/running, the insert is skipped and None is
	returned — same contract as RQ's deduplicate. Old failed rows with the
	id are cleared first so a job can always be re-enqueued after failing.
	"""
	method = queue_args["method"]
	func_name = method if isinstance(method, str) else f"{method.__module__}.{method.__qualname__}"
	payload = pickle.dumps(queue_args, protocol=pickle.HIGHEST_PROTOCOL)

	conn = _connect()
	try:
		conn.execute("DELETE FROM jobs WHERE job_id = ? AND status = 'failed'", (job_id,))
		try:
			conn.execute(
				"INSERT INTO jobs (job_id, site, queue, func, kwargs, max_retries) VALUES (?, ?, ?, ?, ?, ?)",
				(job_id, queue_args["site"], queue, func_name, payload, max_retries),
			)
		except sqlite3.IntegrityError:
			frappe.logger().error(f"Not queueing job {job_id} because it is in queue already")
			return None
	finally:
		conn.close()

	notify_workers()
	return SQLiteJob(job_id)


def is_job_enqueued(job_id: str) -> bool:
	"""``job_id`` already site-namespaced (callers go through create_job_id)."""
	conn = _connect()
	try:
		row = conn.execute(
			"SELECT 1 FROM jobs WHERE job_id = ? AND status IN ('pending', 'running')", (job_id,)
		).fetchone()
		return row is not None
	finally:
		conn.close()


def notify_workers():
	"""Wake the in-loop workers (thread-safe). No-op when no workers run in
	this process — rows are then picked up by the POLL_INTERVAL fallback."""
	loop, event = _state["loop"], _state["event"]
	if loop is not None and event is not None and _state["pid"] == os.getpid() and not loop.is_closed():
		loop.call_soon_threadsafe(event.set)


# --- consumer side (asyncio tasks on the main loop) --------------------------


async def start_workers(num_workers: int | None = None):
	"""Start the worker tasks on the running loop (ASGI lifespan startup)."""
	import aiosqlite

	from frappe.database.aio import run_in_clean_context

	if num_workers is None:
		num_workers = DEFAULT_WORKERS

	# clean context (Phase 6 lesson): aiosqlite's worker thread captures the
	# ambient contextvars Context at creation and would pin it forever
	db_path = get_queue_db_path()
	conn = await run_in_clean_context(_aconnect(db_path))

	# reaper: every worker lives in this process, so any 'running' row at
	# startup belonged to a process that died mid-job — requeue it
	requeued = await conn.execute_fetchall(
		"UPDATE jobs SET status='pending', started_at=NULL WHERE status='running' RETURNING id"
	)
	if requeued:
		frappe.logger("sqlite_queue").warning(f"requeued {len(requeued)} stuck job(s) from previous run")

	event = asyncio.Event()
	with _state_lock:
		_state.update(loop=asyncio.get_running_loop(), event=event, conn=conn, pid=os.getpid())
		_state["tasks"] = [
			asyncio.create_task(_worker(), name=f"sqlite-queue-{i}") for i in range(num_workers)
		]


async def _aconnect(db_path):
	import aiosqlite

	conn = await aiosqlite.connect(db_path, isolation_level=None)
	await conn.executescript(_apply_pragmas_sql() + SCHEMA)
	conn.row_factory = None
	return conn


async def stop_workers():
	"""Cancel workers and close the queue connection (lifespan shutdown)."""
	tasks, conn = _state["tasks"], _state["conn"]
	for t in tasks:
		t.cancel()
	if tasks:
		await asyncio.gather(*tasks, return_exceptions=True)
	if conn is not None:
		with contextlib.suppress(Exception):
			await conn.close()
	with _state_lock:
		_state.update(loop=None, event=None, conn=None, tasks=[], pid=None)


async def _worker():
	event = _state["event"]
	while True:
		try:
			event.clear()
			# drain: claim until the table is empty
			while (row := await _claim()) is not None:
				await _run_job(row)
			with contextlib.suppress(TimeoutError):
				await asyncio.wait_for(event.wait(), timeout=POLL_INTERVAL)
		except asyncio.CancelledError:
			raise
		except Exception:
			# a broken claim/status write must not kill the worker task
			frappe.logger("sqlite_queue").error("worker iteration failed", exc_info=True)
			await asyncio.sleep(1)


async def _claim():
	"""Atomically claim the oldest pending job. Single UPDATE..RETURNING in
	autocommit — no SELECT-then-UPDATE window, two workers can't get one row
	(and aiosqlite serializes statements on this shared connection anyway)."""
	rows = await _state["conn"].execute_fetchall(
		"UPDATE jobs SET status='running', started_at=datetime('now') "
		"WHERE id = (SELECT id FROM jobs WHERE status='pending' ORDER BY id LIMIT 1) "
		"RETURNING id, func, kwargs, retries, max_retries"
	)
	return rows[0] if rows else None


def _make_runner(method, func_name):
	"""Wrap the job function so all resolution/bridging happens INSIDE
	execute_job's per-job site context (pool thread) — this worker coroutine
	runs context-free, where frappe.get_attr would AttributeError (it reads
	frappe.local.flags; caught live, the unit tests' ambient context hid it).
	dispatch_sync runs sync funcs inline and bridges ``async def`` funcs back
	to the main loop — awaited, not dropped."""

	def runner(**kwargs):
		resolved = frappe.get_attr(method) if isinstance(method, str) else method
		return dispatch_sync(resolved, **kwargs)

	# keep execute_job's readable method_name (module.qualname of the real func)
	runner.__module__, _, runner.__qualname__ = func_name.rpartition(".")
	return runner


async def _run_job(row):
	from frappe.database.aio import run_in_clean_context
	from frappe.utils.background_jobs import execute_job

	rowid, func_name, payload, retries, max_retries = row

	try:
		queue_args = pickle.loads(payload)  # same trust model as RQ's pickled args

		# fresh empty contextvars Context per job: the worker tasks all share
		# the lifespan-startup context (create_task copies it, the frappe.local
		# dict inside is the same object) — execute_job's frappe.init/destroy
		# from two concurrent jobs would fight over one frappe.local. In a
		# clean context, frappe.local lazily becomes a new per-job dict.
		await run_in_clean_context(
			sync_to_async(execute_job, thread_sensitive=False)(
				site=queue_args["site"],
				method=_make_runner(queue_args["method"], func_name),
				event=queue_args.get("event"),
				job_name=queue_args.get("job_name"),
				kwargs=queue_args.get("kwargs") or {},
				user=queue_args.get("user"),
				is_async=True,
			)
		)
	except Exception:
		await _record_failure(rowid, retries, max_retries, frappe.get_traceback())
	else:
		await _state["conn"].execute("DELETE FROM jobs WHERE id = ?", (rowid,))


async def _record_failure(rowid, retries, max_retries, traceback):
	retries += 1
	if retries >= max_retries:
		await _state["conn"].execute(
			"UPDATE jobs SET status='failed', retries=?, error=?, ended_at=datetime('now') WHERE id = ?",
			(retries, traceback, rowid),
		)
	else:
		await _state["conn"].execute(
			"UPDATE jobs SET status='pending', retries=?, error=?, started_at=NULL WHERE id = ?",
			(retries, traceback, rowid),
		)
