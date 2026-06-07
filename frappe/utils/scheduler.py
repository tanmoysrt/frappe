# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""
Events:
	always
	daily
	monthly
	weekly
"""

import datetime
import os
import random
import time
from typing import NoReturn

from croniter import CroniterBadCronError
from filelock import FileLock, Timeout

import frappe
from frappe.utils import cint, get_bench_path, get_datetime, get_sites, now_datetime
from frappe.utils.background_jobs import set_niceness
from frappe.utils.caching import redis_cache

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_SCHEDULER_TICK = 4 * 60


def cprint(*args, **kwargs):
	"""Prints only if called from STDOUT"""
	try:
		os.get_terminal_size()
		print(*args, **kwargs)
	except Exception:
		pass


def start_scheduler() -> NoReturn:
	"""Run enqueue_events_for_all_sites based on scheduler tick.
	Specify scheduler_tick_interval in seconds in common_site_config.json"""

	tick = get_scheduler_tick()
	set_niceness()

	lock_path = _get_scheduler_lock_file()

	try:
		lock = FileLock(lock_path)
		lock.acquire(blocking=False)
	except Timeout:
		frappe.logger("scheduler").debug("Scheduler already running")
		return

	while True:
		time.sleep(sleep_duration(tick))
		enqueue_events_for_all_sites()


def _get_scheduler_lock_file() -> True:
	return os.path.abspath(os.path.join(get_bench_path(), "config", "scheduler_process"))


def is_schduler_process_running() -> bool:
	"""Checks if any other process is holding the lock.

	Note: FLOCK is held by process until it exits, this function just checks if process is
	running or not. We can't determine if process is stuck somehwere.
	"""
	try:
		lock = FileLock(_get_scheduler_lock_file())
		lock.acquire(blocking=False)
		lock.release()
		return False
	except Timeout:
		return True


def sleep_duration(tick):
	if tick != DEFAULT_SCHEDULER_TICK:
		# Assuming user knows what they want.
		return tick

	# Sleep until next multiple of tick.
	# This makes scheduler aligned with real clock,
	# so event scheduled at 12:00 happen at 12:00 and not 12:00:35.
	minutes = tick // 60
	now = datetime.datetime.now(datetime.UTC)
	left_minutes = minutes - now.minute % minutes
	next_execution = now.replace(second=0) + datetime.timedelta(minutes=left_minutes)

	return (next_execution - now).total_seconds()


# --- in-process scheduler (Phase 11) -----------------------------------------
#
# Same tick loop as start_scheduler, as an asyncio task on the main event
# loop (started at ASGI lifespan startup) — no separate scheduler process in
# light mode. Everything that matters is shared with the process scheduler:
# tick interval + wall-clock alignment (sleep_duration), the cross-process
# FileLock (an external `bench schedule` and the in-process task can never
# both run), per-site iteration/enqueue and the scheduler_disabled flags
# (enqueue_events_for_all_sites, unchanged). Config flip reverts:
# `in_process_scheduler: 0` in common config + run `bench schedule` as today.

_task_state = {"task": None, "lock": None}


def in_process_scheduler_enabled() -> bool:
	return cint(frappe.get_common_conf("in_process_scheduler", 1)) == 1


def start_scheduler_task() -> None:
	"""Create the scheduler tick task on the running loop (lifespan startup)."""
	import asyncio

	_task_state["task"] = asyncio.create_task(_scheduler_loop(), name="frappe-scheduler")


async def stop_scheduler_task() -> None:
	import asyncio

	if task := _task_state["task"]:
		task.cancel()
		import contextlib

		with contextlib.suppress(asyncio.CancelledError):
			await task
	_task_state["task"] = None


async def _scheduler_loop():
	import asyncio

	from asgiref.sync import sync_to_async

	from frappe.database.aio import run_in_clean_context

	def _setup():
		# sync setup in a pool thread, clean context: config read + the same
		# cross-process FileLock start_scheduler takes. thread_local=False —
		# acquired here, released from whatever thread runs the finally.
		# No set_niceness: that would renice the whole web process.
		if not in_process_scheduler_enabled():
			return None, None
		lock = FileLock(_get_scheduler_lock_file(), thread_local=False)
		try:
			lock.acquire(blocking=False)
		except Timeout:
			frappe.logger("scheduler").info(
				"scheduler lock held (external bench schedule?) — in-process scheduler not started"
			)
			return None, None
		return lock, get_scheduler_tick()

	lock, tick = await run_in_clean_context(sync_to_async(_setup, thread_sensitive=False)())
	if lock is None:
		return
	_task_state["lock"] = lock

	try:
		while True:
			await asyncio.sleep(sleep_duration(tick))
			try:
				# heavy sync work (per-site init/connect/enqueue/destroy) on the
				# pool, in a fresh contextvars Context per tick — never on the
				# loop thread, never sharing the lifespan context's frappe.local
				await run_in_clean_context(
					sync_to_async(enqueue_events_for_all_sites, thread_sensitive=False)()
				)
			except Exception:
				frappe.logger("scheduler").error("in-process scheduler tick failed", exc_info=True)
	finally:
		lock.release()
		_task_state["lock"] = None


def enqueue_events_for_all_sites() -> None:
	"""Loop through sites and enqueue events that are not already queued"""

	with frappe.init_site():
		sites = get_sites()

	# Sites are sorted in alphabetical order, shuffle to randomize priorities
	random.shuffle(sites)

	for site in sites:
		try:
			enqueue_events_for_site(site=site)
		except Exception:
			frappe.logger("scheduler").debug(f"Failed to enqueue events for site: {site}", exc_info=True)


def enqueue_events_for_site(site: str) -> None:
	def log_exc():
		frappe.logger("scheduler").error(f"Exception in Enqueue Events for Site {site}", exc_info=True)

	try:
		frappe.init(site)
		frappe.connect()
		if is_scheduler_inactive():
			return

		enqueue_events()

		frappe.logger("scheduler").debug(f"Queued events for site {site}")
	except Exception as e:
		if frappe.db.is_access_denied(e):
			frappe.logger("scheduler").debug(f"Access denied for site {site}")
		log_exc()

	finally:
		frappe.destroy()


def enqueue_events() -> list[str] | None:
	if schedule_jobs_based_on_activity():
		enqueued_jobs = []
		all_jobs = frappe.get_docs("Scheduled Job Type", filters={"stopped": 0})
		random.shuffle(all_jobs)
		for job_type in all_jobs:
			try:
				if job_type.enqueue():
					enqueued_jobs.append(job_type.method)
			except CroniterBadCronError:
				frappe.logger("scheduler").error(
					f"Invalid Job on {frappe.local.site} - {job_type.name}", exc_info=True
				)

		return enqueued_jobs


def is_scheduler_inactive(verbose=True) -> bool:
	if frappe.local.conf.maintenance_mode:
		if verbose:
			cprint(f"{frappe.local.site}: Maintenance mode is ON")
		return True

	if frappe.local.conf.pause_scheduler:
		if verbose:
			cprint(f"{frappe.local.site}: frappe.conf.pause_scheduler is SET")
		return True

	if is_scheduler_disabled(verbose=verbose):
		return True

	return False


def is_scheduler_disabled(verbose=True) -> bool:
	if frappe.conf.disable_scheduler:
		if verbose:
			cprint(f"{frappe.local.site}: frappe.conf.disable_scheduler is SET")
		return True

	scheduler_disabled = not frappe.get_system_settings("enable_scheduler")
	if scheduler_disabled:
		if verbose:
			cprint(f"{frappe.local.site}: SystemSettings.enable_scheduler is UNSET")
	return scheduler_disabled


def toggle_scheduler(enable):
	frappe.db.set_single_value("System Settings", "enable_scheduler", int(enable))


def enable_scheduler():
	toggle_scheduler(True)


def disable_scheduler():
	toggle_scheduler(False)


@redis_cache(ttl=60 * 60)
def schedule_jobs_based_on_activity(check_time=None):
	"""Return True for active sites as defined by `Activity Log`.
	Also return True for inactive sites once every 24 hours based on `Scheduled Job Log`."""
	if is_dormant(check_time=check_time):
		# ensure last job is one day old
		last_job_timestamp = _get_last_creation_timestamp("Scheduled Job Log")
		if not last_job_timestamp:
			return True
		else:
			if ((check_time or now_datetime()) - last_job_timestamp).total_seconds() >= 86400:
				# one day is passed since jobs are run, so lets do this
				return True
			else:
				# schedulers run in the last 24 hours, do nothing
				return False
	else:
		# site active, lets run the jobs
		return True


@redis_cache(ttl=60 * 60)
def is_dormant(check_time=None):
	from frappe.utils.frappecloud import on_frappecloud

	if frappe.conf.developer_mode or not on_frappecloud():
		return False
	threshold = cint(frappe.get_system_settings("dormant_days")) * 86400
	if not threshold:
		return False

	last_activity = frappe.db.get_value(
		"User", filters={}, fieldname="last_active", order_by="last_active desc"
	)

	if not last_activity:
		return True
	if ((check_time or now_datetime()) - last_activity).total_seconds() >= threshold:
		return True
	return False


def _get_last_creation_timestamp(doctype):
	timestamp = frappe.db.get_value(doctype, filters={}, fieldname="creation", order_by="creation desc")
	if timestamp:
		return get_datetime(timestamp)


@frappe.whitelist()
def activate_scheduler():
	from frappe.installer import update_site_config

	frappe.only_for("Administrator")

	if frappe.local.conf.maintenance_mode:
		frappe.throw(frappe._("Scheduler can not be re-enabled when maintenance mode is active."))

	if is_scheduler_disabled():
		enable_scheduler()
	if frappe.conf.pause_scheduler:
		update_site_config("pause_scheduler", 0)


@frappe.whitelist()
def get_scheduler_status():
	if is_scheduler_inactive():
		return {"status": "inactive"}
	return {"status": "active"}


def get_scheduler_tick() -> int:
	conf = frappe.get_conf()
	return cint(conf.scheduler_tick_interval) or DEFAULT_SCHEDULER_TICK
