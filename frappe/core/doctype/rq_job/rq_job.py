# Copyright (c) 2022, Frappe Technologies and contributors
# For license information, please see license.txt

# Phase 24.1/24.4: rq is imported function-level (future-annotations frees the
# Job/Queue type hints) so a light site on the sqlite queue never pulls rq just
# to open the RQ Job list. When queue_backend == "sqlite", get_list/load_from_db
# read the sqlite `jobs` table instead of redis.
from __future__ import annotations

import functools
import re
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
	cint,
	compare,
	convert_utc_to_system_timezone,
	create_batch,
	get_datetime,
	make_filter_dict,
)

if TYPE_CHECKING:
	from rq.job import Job
	from rq.queue import Queue

QUEUES = ["default", "long", "short"]
JOB_STATUSES = ["queued", "started", "failed", "finished", "deferred", "scheduled", "canceled"]

# sqlite `jobs` table only ever holds pending/running/failed (successful jobs are
# deleted on completion); map those onto the RQ Job status vocabulary.
SQLITE_STATUS_MAP = {"pending": "queued", "running": "started", "failed": "failed"}


def _is_sqlite_queue() -> bool:
	return frappe.get_common_conf("queue_backend") == "sqlite"


def check_permissions(method):
	@functools.wraps(method)
	def wrapper(*args, **kwargs):
		frappe.only_for("System Manager")
		if _is_sqlite_queue():
			# read-only on the sqlite queue (24.4); load_from_db already
			# scoped the row to the current site.
			frappe.msgprint(
				_("Job actions are not available on the SQLite queue (read-only)."),
				title=_("Not Supported"),
			)
			return
		job = args[0].job
		if not for_current_site(job):
			raise frappe.PermissionError

		return method(*args, **kwargs)

	return wrapper


class RQJob(Document):
	_DOCTYPE_NAME = "RQ Job"

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		arguments: DF.Code | None
		ended_at: DF.Datetime | None
		exc_info: DF.Code | None
		job_id: DF.Data | None
		job_name: DF.Data | None
		queue: DF.Literal["default", "short", "long"]
		started_at: DF.Datetime | None
		status: DF.Literal["queued", "started", "finished", "failed", "deferred", "scheduled", "canceled"]
		time_taken: DF.Duration | None
		timeout: DF.Duration | None
	# end: auto-generated types

	def load_from_db(self):
		if _is_sqlite_queue():
			row = _sqlite_fetch_one(self.name)
			if row is None:
				raise frappe.DoesNotExistError
			super(Document, self).__init__(serialize_sqlite_job(row))
			self._job_obj = None
			return

		from rq.exceptions import NoSuchJobError
		from rq.job import Job

		from frappe.utils.background_jobs import get_redis_conn

		try:
			job = Job.fetch(self.name, connection=get_redis_conn())
		except NoSuchJobError:
			raise frappe.DoesNotExistError

		if not for_current_site(job):
			raise frappe.PermissionError

		super(Document, self).__init__(serialize_job(job))
		self._job_obj = job

	@property
	def job(self):
		return self._job_obj

	@staticmethod
	def get_list(filters=None, start=0, page_length=20, order_by="creation desc"):
		if _is_sqlite_queue():
			return _sqlite_get_list(filters, start, page_length, order_by)

		from rq.job import Job

		from frappe.utils.background_jobs import get_redis_conn

		matched_job_ids = RQJob.get_matching_job_ids(filters=filters)[start : start + page_length]

		conn = get_redis_conn()
		jobs = [serialize_job(job) for job in Job.fetch_many(job_ids=matched_job_ids, connection=conn) if job]

		order_desc = "desc" in order_by
		return sorted(jobs, key=lambda j: j.creation, reverse=order_desc)

	@staticmethod
	def get_matching_job_ids(filters) -> list[str]:
		from frappe.utils.background_jobs import get_queues

		filters = make_filter_dict(filters or [])

		queues = _eval_filters(filters.get("queue"), QUEUES + get_custom_queues())
		statuses = _eval_filters(filters.get("status"), JOB_STATUSES)

		matched_job_ids = []
		for queue in get_queues():
			if not queue.name.endswith(tuple(queues)):
				continue
			for status in statuses:
				matched_job_ids.extend(fetch_job_ids(queue, status))

		return filter_current_site_jobs(matched_job_ids)

	@check_permissions
	def delete(self):
		self.job.delete()

	@check_permissions
	def stop_job(self):
		from rq.command import send_stop_job_command
		from rq.exceptions import InvalidJobOperation

		from frappe.utils.background_jobs import get_redis_conn

		try:
			send_stop_job_command(connection=get_redis_conn(), job_id=self.job_id)
		except InvalidJobOperation:
			frappe.msgprint(_("Job is not running."), title=_("Invalid Operation"))

	@check_permissions
	def cancel(self):
		if self.status == "queued":
			self.job.cancel()
		else:
			frappe.msgprint(
				_("Job is in {0} state and can't be cancelled").format(self.status),
				title=_("Invalid Operation"),
			)

	@staticmethod
	def get_count(filters=None) -> int:
		if _is_sqlite_queue():
			return len(_sqlite_get_list(filters, 0, 1_000_000, "creation desc"))
		return len(RQJob.get_matching_job_ids(filters))

	# None of these methods apply to virtual job doctype, overriden for sanity.
	@staticmethod
	def get_stats():
		return {}

	def db_insert(self, *args, **kwargs):
		pass

	def db_update(self, *args, **kwargs):
		pass


def serialize_job(job: Job) -> frappe._dict:
	modified = job.last_heartbeat or job.ended_at or job.started_at or job.created_at
	job_kwargs = job.kwargs.get("kwargs", {})
	job_name = job_kwargs.get("job_type") or str(job.kwargs.get("job_name"))
	if job_name == "frappe.utils.background_jobs.run_doc_method":
		doctype = job_kwargs.get("doctype")
		doc_method = job_kwargs.get("doc_method")
		if doctype and doc_method:
			job_name = f"{doctype}.{doc_method}"

	# function objects have this repr: '<function functionname at 0xmemory_address >'
	# This regex just removes unnecessary things around it.
	if matches := re.match(r"<function (?P<func_name>.*) at 0x.*>", job_name):
		job_name = matches.group("func_name")

	exc_info = None

	# Get exc_string from the job result if it exists
	if job_result := job.latest_result():
		exc_info = job_result.exc_string

	return frappe._dict(
		name=job.id,
		job_id=job.id,
		queue=job.origin.rsplit(":", 1)[1],
		job_name=job_name,
		status=job.get_status(),
		started_at=convert_utc_to_system_timezone(job.started_at) if job.started_at else "",
		ended_at=convert_utc_to_system_timezone(job.ended_at) if job.ended_at else "",
		time_taken=(job.ended_at - job.started_at).total_seconds() if job.ended_at else "",
		exc_info=exc_info,
		arguments=frappe.as_json(job.kwargs),
		timeout=job.timeout,
		creation=convert_utc_to_system_timezone(job.created_at),
		modified=convert_utc_to_system_timezone(modified),
		_comment_count=0,
		owner=job.kwargs.get("user"),
		modified_by=job.kwargs.get("user"),
	)


def serialize_sqlite_job(row) -> frappe._dict:
	"""Map a sqlite `jobs` row onto the RQ Job fields (24.4). Timestamps are
	stored as UTC text; kwargs is the pickled queue_args dict (best-effort)."""
	import pickle

	status = SQLITE_STATUS_MAP.get(row["status"], row["status"])
	job_name = row["func"]
	user = None
	arguments = ""
	try:
		queue_args = pickle.loads(row["kwargs"])
		user = queue_args.get("user")
		arguments = frappe.as_json(queue_args)
		job_name = queue_args.get("job_name") or job_name
	except Exception:
		pass

	def conv(text):
		return convert_utc_to_system_timezone(get_datetime(text)) if text else ""

	started = get_datetime(row["started_at"]) if row["started_at"] else None
	ended = get_datetime(row["ended_at"]) if row["ended_at"] else None
	time_taken = (ended - started).total_seconds() if (started and ended) else ""

	return frappe._dict(
		name=row["job_id"],
		job_id=row["job_id"],
		queue=row["queue"],
		job_name=job_name,
		status=status,
		started_at=conv(row["started_at"]),
		ended_at=conv(row["ended_at"]),
		time_taken=time_taken,
		exc_info=row["error"],
		arguments=arguments,
		timeout="",
		creation=conv(row["enqueued_at"]),
		modified=conv(row["ended_at"] or row["started_at"] or row["enqueued_at"]),
		_comment_count=0,
		owner=user,
		modified_by=user,
	)


def _sqlite_conn():
	from frappe.utils import sqlite_queue

	return sqlite_queue._connect()


def _sqlite_fetch_one(job_id: str):
	conn = _sqlite_conn()
	try:
		return conn.execute(
			"SELECT * FROM jobs WHERE site = ? AND job_id = ?",
			(frappe.local.site, job_id),
		).fetchone()
	finally:
		conn.close()


def _sqlite_match(job: frappe._dict, filters) -> bool:
	fd = make_filter_dict(filters or [])
	for field in ("status", "queue", "job_id", "job_name"):
		flt = fd.get(field)
		if flt:
			operator, operand = flt
			if not compare(job.get(field), operator, operand):
				return False
	return True


def _sqlite_get_list(filters=None, start=0, page_length=20, order_by="creation desc"):
	conn = _sqlite_conn()
	try:
		rows = conn.execute(
			"SELECT * FROM jobs WHERE site = ? ORDER BY id DESC", (frappe.local.site,)
		).fetchall()
	finally:
		conn.close()

	jobs = [serialize_sqlite_job(r) for r in rows]
	jobs = [j for j in jobs if _sqlite_match(j, filters)]
	jobs.sort(key=lambda j: j.creation or "", reverse="desc" in order_by)
	return jobs[start : start + page_length]


def _sqlite_remove_failed():
	conn = _sqlite_conn()
	try:
		conn.execute("DELETE FROM jobs WHERE site = ? AND status = 'failed'", (frappe.local.site,))
	finally:
		conn.close()


def for_current_site(job: Job) -> bool:
	return job.kwargs.get("site") == frappe.local.site


def filter_current_site_jobs(job_ids: list[str]) -> list[str]:
	site = frappe.local.site

	return [j for j in job_ids if j.startswith(site)]


def _eval_filters(filter, values: list[str]) -> list[str]:
	if filter:
		operator, operand = filter
		return [val for val in values if compare(val, operator, operand)]
	return values


def fetch_job_ids(queue: Queue, status: str) -> list[str]:
	from rq.queue import Queue

	registry_map = {
		"queued": queue,  # self
		"started": queue.started_job_registry,
		"finished": queue.finished_job_registry,
		"failed": queue.failed_job_registry,
		"deferred": queue.deferred_job_registry,
		"scheduled": queue.scheduled_job_registry,
		"canceled": queue.canceled_job_registry,
	}

	registry = registry_map.get(status)
	if registry is not None:
		if isinstance(registry, Queue):
			job_ids = registry.get_job_ids()
		else:
			job_ids = registry.get_job_ids(cleanup=False)
		return [j for j in job_ids if j]

	return []


@frappe.whitelist()
def remove_failed_jobs():
	frappe.only_for("System Manager")
	if _is_sqlite_queue():
		_sqlite_remove_failed()
		return

	from rq.job import Job

	from frappe.utils.background_jobs import get_queues, get_redis_conn

	for queue in get_queues():
		fail_registry = queue.failed_job_registry
		failed_jobs = filter_current_site_jobs(fail_registry.get_job_ids(cleanup=False))

		# Delete in batches to avoid loading too many things in memory
		conn = get_redis_conn()
		for job_ids in create_batch(failed_jobs, 100):
			for job in Job.fetch_many(job_ids=job_ids, connection=conn):
				job and fail_registry.remove(job, delete_job=True)


def get_all_queued_jobs():
	from frappe.utils.background_jobs import get_queues

	jobs = []
	for q in get_queues():
		jobs.extend(q.get_jobs())

	return [job for job in jobs if for_current_site(job)]


@frappe.whitelist()
def stop_job(job_id: str):
	frappe.get_doc("RQ Job", job_id).stop_job()


@frappe.whitelist()
def get_custom_queues():
	frappe.has_permission("RQ Job", throw=True)
	return list((frappe.conf.workers or {}).keys())
