# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Python socket.io server (Phase 13) — runs in the same uvicorn loop as HTTP.

Port of apps/frappe/realtime (Node.js): same namespaces (one per site), same
rooms, same event names, same auth semantics. Differences by design:

- Auth is a direct in-process call (frappe.init + LoginManager session
  resume + validate_auth in a pool thread) instead of the HTTP round-trip
  Node makes to /api/method/frappe.realtime.get_user_info — so the socketio
  secret handshake isn't needed here.
- The origin check only runs when the client sends an Origin header
  (browsers always do; non-browser clients aren't forced to fake one).

Mounted in frappe/asgi.py under /socket.io for both http (long-polling) and
websocket scopes. An asyncio task subscribes to the Redis "events" channel
for out-of-process publishers (bench CLI, RQ workers). The Node.js realtime
server this ports was removed in Phase 18.
"""

import asyncio
import contextlib
import contextvars
import json
import os

import socketio
from asgiref.sync import sync_to_async
from socketio.exceptions import ConnectionRefusedError as SioConnectionRefused

import frappe
from frappe.database.aio import run_in_clean_context
from frappe.realtime import (
	get_doc_room,
	get_doctype_room,
	get_site_room,
	get_task_progress_room,
	get_user_room,
)

WEBSITE_ROOM = "website"

sio = socketio.AsyncServer(
	async_mode="asgi",
	namespaces="*",  # one namespace per site, validated in connect
	cors_allowed_origins="*",  # origin validated explicitly below, like Node
)
application = socketio.ASGIApp(sio, socketio_path="socket.io")

_state = {"subscriber": None, "loop": None, "pid": None}


def open_doc_room(doctype, docname):
	return f"open_doc:{doctype}/{docname}"


def _hostname(addr):
	if not addr:
		return None
	return addr.split("://")[-1].split("/")[0].split(":")[0].lower()


async def _pool(fn, *args):
	"""Sync frappe work: pool thread, fresh contextvars Context (the handler
	runs on the loop in a long-lived socket context — Phase 6/9 lesson)."""
	return await run_in_clean_context(sync_to_async(fn, thread_sensitive=False)(*args))


# --- sync helpers (pool threads) ---------------------------------------------


def _resolve_user(site, cookie, authorization):
	"""The exact auth an HTTP request gets — LoginManager session resume from
	the sid cookie (HTTPRequest) + Authorization header (validate_auth) —
	without the HTTP round-trip."""
	from frappe.auth import HTTPRequest, validate_auth
	from frappe.utils import set_request

	headers = {}
	if cookie:
		headers["Cookie"] = cookie
	if authorization:
		headers["Authorization"] = authorization

	frappe.init(site, force=True)
	set_request(method="GET", path="/api/method/frappe.realtime.get_user_info", headers=headers)
	try:
		frappe.connect()
		HTTPRequest()
		validate_auth()
		user = frappe.session.user
		user_type = frappe.session.data.user_type or frappe.get_cached_value("User", user, "user_type")
		return {"user": user, "user_type": user_type}
	finally:
		frappe.destroy()


def _check_permission(site, user, doctype, name):
	frappe.init(site, force=True)
	try:
		frappe.connect()
		frappe.set_user(user)
		return bool(frappe.has_permission(doctype, doc=name or None))
	except Exception:
		return False
	finally:
		frappe.destroy()


# --- connection ----------------------------------------------------------------


@sio.on("connect", namespace="*")
async def connect(namespace, sid, environ, auth):
	site = namespace.removeprefix("/")

	host = environ.get("HTTP_HOST")
	origin = environ.get("HTTP_ORIGIN")
	if origin and _hostname(origin) != _hostname(host):
		raise SioConnectionRefused("Invalid origin")

	# the namespace must be the site this connection is for (Node: explicit
	# header, else origin/host)
	claimed = environ.get("HTTP_X_FRAPPE_SITE_NAME") or origin or host
	if _hostname(claimed) != site.lower():
		raise SioConnectionRefused("Invalid namespace")

	cookie = environ.get("HTTP_COOKIE")
	authorization = environ.get("HTTP_AUTHORIZATION")
	if not cookie and not authorization:
		raise SioConnectionRefused("No authentication method used. Use cookie or authorization header.")

	try:
		info = await _pool(_resolve_user, site, cookie, authorization)
	except Exception as e:
		raise SioConnectionRefused("Unauthorized") from e

	await sio.save_session(
		sid,
		{"site": site, "user": info["user"], "user_type": info["user_type"], "open_docs": set()},
		namespace=namespace,
	)
	await sio.enter_room(sid, get_user_room(info["user"]), namespace=namespace)
	await sio.enter_room(sid, WEBSITE_ROOM, namespace=namespace)
	if info["user_type"] == "System User":
		await sio.enter_room(sid, get_site_room(), namespace=namespace)


@sio.on("disconnect", namespace="*")
async def disconnect(namespace, sid, reason=None):
	with contextlib.suppress(KeyError):
		session = await sio.get_session(sid, namespace=namespace)
		for doctype, docname in session.get("open_docs") or ():
			await _notify_doc_viewers(namespace, doctype, docname, exclude_sid=sid)


# --- event handlers (same names/behavior as realtime/handlers.js) -------------


@sio.on("ping", namespace="*")
async def ping(namespace, sid):
	await sio.emit("pong", to=sid, namespace=namespace)


@sio.on("doctype_subscribe", namespace="*")
async def doctype_subscribe(namespace, sid, doctype):
	session = await sio.get_session(sid, namespace=namespace)
	if await _pool(_check_permission, session["site"], session["user"], doctype, None):
		await sio.enter_room(sid, get_doctype_room(doctype), namespace=namespace)


@sio.on("doctype_unsubscribe", namespace="*")
async def doctype_unsubscribe(namespace, sid, doctype):
	await sio.leave_room(sid, get_doctype_room(doctype), namespace=namespace)


@sio.on("task_subscribe", namespace="*")
async def task_subscribe(namespace, sid, task_id):
	await sio.enter_room(sid, get_task_progress_room(task_id), namespace=namespace)


@sio.on("progress_subscribe", namespace="*")
async def progress_subscribe(namespace, sid, task_id):
	await sio.enter_room(sid, get_task_progress_room(task_id), namespace=namespace)


@sio.on("task_unsubscribe", namespace="*")
async def task_unsubscribe(namespace, sid, task_id):
	await sio.leave_room(sid, get_task_progress_room(task_id), namespace=namespace)


@sio.on("doc_subscribe", namespace="*")
async def doc_subscribe(namespace, sid, doctype, docname):
	session = await sio.get_session(sid, namespace=namespace)
	if await _pool(_check_permission, session["site"], session["user"], doctype, docname):
		await sio.enter_room(sid, get_doc_room(doctype, docname), namespace=namespace)


@sio.on("doc_unsubscribe", namespace="*")
async def doc_unsubscribe(namespace, sid, doctype, docname):
	await sio.leave_room(sid, get_doc_room(doctype, docname), namespace=namespace)


@sio.on("doc_open", namespace="*")
async def doc_open(namespace, sid, doctype, docname):
	session = await sio.get_session(sid, namespace=namespace)
	if not await _pool(_check_permission, session["site"], session["user"], doctype, docname):
		return
	await sio.enter_room(sid, open_doc_room(doctype, docname), namespace=namespace)
	session["open_docs"].add((doctype, docname))
	await sio.save_session(sid, session, namespace=namespace)
	await _notify_doc_viewers(namespace, doctype, docname)


@sio.on("doc_close", namespace="*")
async def doc_close(namespace, sid, doctype, docname):
	await sio.leave_room(sid, open_doc_room(doctype, docname), namespace=namespace)
	with contextlib.suppress(KeyError):
		session = await sio.get_session(sid, namespace=namespace)
		session["open_docs"].discard((doctype, docname))
		await sio.save_session(sid, session, namespace=namespace)
	await _notify_doc_viewers(namespace, doctype, docname)


async def _notify_doc_viewers(namespace, doctype, docname, exclude_sid=None):
	"""Emit the current viewer list to everyone with the doc open
	(handlers.js notify_subscribed_doc_users)."""
	room = open_doc_room(doctype, docname)
	users = set()
	with contextlib.suppress(KeyError):
		for psid, _eio_sid in sio.manager.get_participants(namespace, room):
			if psid == exclude_sid:
				continue
			with contextlib.suppress(KeyError):
				psession = await sio.get_session(psid, namespace=namespace)
				users.add(psession["user"])
	await sio.emit(
		"doc_viewers",
		{"doctype": doctype, "docname": docname, "users": sorted(users)},
		room=room,
		namespace=namespace,
	)


# --- direct emit (Phase 14) ----------------------------------------------------
#
# Single process, single loop, every client connected right here — so
# publish_realtime from this process emits straight onto the loop, no Redis
# pub/sub. The Redis subscriber below stays for OUT-of-process publishers
# (bench CLI, RQ workers in heavy mode) and as the Node-revert path.


def is_active() -> bool:
	"""True when the in-process socket.io server runs in THIS process."""
	loop = _state["loop"]
	return loop is not None and _state["pid"] == os.getpid() and not loop.is_closed()


def emit_threadsafe(event, message, room, site):
	"""Schedule an emit onto the server loop from any thread (pool threads
	running requests/jobs, or the loop itself). Fire-and-forget — delivery
	failures are logged, never raised into the publisher."""
	asyncio.run_coroutine_threadsafe(_emit(event, message, room, site), _state["loop"])


async def _emit(event, message, room, site):
	try:
		await sio.emit(event, message, room=room or None, namespace="/" + site)
	except Exception:
		frappe.logger("realtime").error("direct realtime emit failed", exc_info=True)


# --- redis "events" subscriber -------------------------------------------------
#
# Out-of-process publishers (bench CLI, workers) still write to the Redis
# "events" channel; this task mirrors realtime/index.js's subscriber so
# those events reach clients connected to this process. Also keeps Python
# and Node in lockstep while both run.


def start():
	"""Start the events subscriber (ASGI lifespan startup). The task runs in
	an empty contextvars Context — its redis connection must not pin the
	startup context."""
	_state["loop"] = asyncio.get_running_loop()
	_state["pid"] = os.getpid()
	_state["subscriber"] = _state["loop"].create_task(_events_subscriber(), context=contextvars.Context())
	_state["subscriber"].set_name("frappe-realtime-events")


async def stop():
	if task := _state["subscriber"]:
		task.cancel()
		with contextlib.suppress(asyncio.CancelledError):
			await task
	_state.update(subscriber=None, loop=None, pid=None)


def _redis_queue_url():
	return frappe.get_conf().get("redis_queue") or "redis://127.0.0.1:11001"


async def _events_subscriber():
	import redis.asyncio as aredis

	url = await sync_to_async(_redis_queue_url, thread_sensitive=False)()
	while True:
		client = aredis.from_url(url)
		pubsub = client.pubsub()
		try:
			await pubsub.subscribe("events")
			async for message in pubsub.listen():
				if message["type"] != "message":
					continue
				data = json.loads(message["data"])
				await sio.emit(
					data["event"],
					data["message"],
					room=data.get("room") or None,
					namespace="/" + data["namespace"],
				)
		except asyncio.CancelledError:
			raise
		except Exception:
			frappe.logger("realtime").error("events subscriber failed; reconnecting", exc_info=True)
			await asyncio.sleep(1)
		finally:
			with contextlib.suppress(Exception):
				await pubsub.aclose()
			with contextlib.suppress(Exception):
				await client.aclose()
