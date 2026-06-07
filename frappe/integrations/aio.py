# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Async outbound HTTP (Phase 12) — httpx.AsyncClient for async-primary
call-sites (webhooks, integrations, OAuth flows running in async handlers).

Mirrors frappe.integrations.utils.make_request semantics exactly (response
parsing, frappe.flags.integration_request, error logging) so a call-site
can move between the two by adding/removing an await. Existing `requests`
code is untouched — it keeps running in worker pool threads.

One client per (pid, loop): connections belong to the loop they were opened
on; the main loop gets one long-lived pool (keep-alive reuse), CLI/bridge
contexts get their own. httpx opens connections inside the awaiting task
(no detached reader tasks), so there is no contextvars-pinning hazard here.
"""

import asyncio
import os
from urllib.parse import parse_qs

import frappe

DEFAULT_TIMEOUT = 30

_clients = {}  # (pid, loop) -> httpx.AsyncClient


def get_async_http_client():
	"""Shared AsyncClient for the running loop (call from async code only)."""
	import httpx

	key = (os.getpid(), asyncio.get_running_loop())
	if (client := _clients.get(key)) is None or client.is_closed:
		client = _clients[key] = httpx.AsyncClient(follow_redirects=True, timeout=DEFAULT_TIMEOUT)
	return client


async def make_request_async(
	method: str, url: str, auth=None, headers=None, data=None, json=None, params=None, timeout=None
):
	try:
		client = get_async_http_client()
		response = frappe.flags.integration_request = await client.request(
			method,
			url,
			auth=auth,
			headers=headers,
			data=data,
			json=json,
			params=params,
			timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
		)
		response.raise_for_status()

		# same content-type handling as the sync make_request
		if content_type := response.headers.get("content-type"):
			if content_type == "text/plain; charset=utf-8":
				return parse_qs(response.text)
			elif content_type.startswith("application/") and content_type.split(";")[0].endswith("json"):
				return response.json()
			elif response.text:
				return response.text
		return
	except Exception as exc:
		# error logging writes an Error Log doc — sync DB work, so it must
		# leave the loop thread (the Phase 8 guard rejects it here otherwise)
		from asgiref.sync import sync_to_async

		await sync_to_async(_log_request_error, thread_sensitive=False)()
		raise exc


def _log_request_error():
	if frappe.flags.integration_request_doc:
		frappe.flags.integration_request_doc.log_error()
	else:
		frappe.log_error()


async def make_get_request_async(url: str, **kwargs):
	return await make_request_async("GET", url, **kwargs)


async def make_post_request_async(url: str, **kwargs):
	return await make_request_async("POST", url, **kwargs)


async def make_put_request_async(url: str, **kwargs):
	return await make_request_async("PUT", url, **kwargs)


async def make_patch_request_async(url: str, **kwargs):
	return await make_request_async("PATCH", url, **kwargs)


async def make_delete_request_async(url: str, **kwargs):
	return await make_request_async("DELETE", url, **kwargs)
