# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Async-primary SMTP (Phase 12), aiosmtplib on the bridge loop.

The wire protocol runs async (aiosmtplib), existing call-sites stay sync
and unchanged: ``SMTPServer.session`` returns a :class:`BridgedSMTPSession`
that exposes the smtplib surface the codebase actually uses (sendmail,
noop, quit, has_extn, esmtp_features) and bridges each call to the
process-wide bridge loop — the same adapter pattern as the async DB
backends (frappe/database/aio.py).

Async callers don't need a special facade: ``frappe.sendmail`` is mostly
DB work (Email Queue), so from an async handler use
``await sync_to_async(frappe.sendmail, thread_sensitive=False)(...)``;
the SMTP hop itself is already non-blocking on the loop either way.

The OAuth (XOAUTH2) path keeps smtplib — frappe.email.oauth drives the
raw smtplib session object directly.
"""

import contextvars

from frappe.dispatch import run_coroutine_sync


async def _run_clean(coro):
	"""aiosmtplib's connection lives on the bridge loop; create it in an
	empty contextvars Context so its transport never pins the creating
	request's frappe.local (Phase 6 lesson)."""
	import asyncio

	return await asyncio.get_running_loop().create_task(coro, context=contextvars.Context())


async def _connect(server, port, use_ssl, use_tls, timeout, login, password, ehlo_after_auth):
	"""Connect + (optional) STARTTLS + (optional) LOGIN — all on the loop.
	Raises aiosmtplib.SMTPAuthenticationError / OSError like smtplib does."""
	import aiosmtplib

	smtp = aiosmtplib.SMTP(
		hostname=server,
		port=port,
		timeout=timeout,
		use_tls=bool(use_ssl),
		start_tls=bool(use_tls),
	)
	await smtp.connect()
	if password:
		await smtp.login(str(login or ""), str(password or ""))
		# Re-issue EHLO after AUTH to refresh server capabilities
		if ehlo_after_auth:
			await smtp.ehlo()
	return smtp


def connect(server, port, use_ssl, use_tls, timeout, login, password, ehlo_after_auth=True):
	"""Sync entrypoint for SMTPServer: returns a BridgedSMTPSession."""
	asmtp = run_coroutine_sync(
		_run_clean(_connect(server, port, use_ssl, use_tls, timeout, login, password, ehlo_after_auth))
	)
	return BridgedSMTPSession(asmtp)


class BridgedSMTPSession:
	"""Sync smtplib-shaped facade over an aiosmtplib.SMTP connection."""

	def __init__(self, asmtp):
		self._asmtp = asmtp

	def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
		errors, _response = run_coroutine_sync(
			self._asmtp.sendmail(
				from_addr, to_addrs, msg, mail_options=list(mail_options), rcpt_options=list(rcpt_options)
			)
		)
		# smtplib returns just the refused-recipients dict
		return {rcpt: (resp.code, resp.message) for rcpt, resp in errors.items()}

	def noop(self):
		response = run_coroutine_sync(self._asmtp.noop())
		return (response.code, response.message)

	def quit(self):
		response = run_coroutine_sync(self._asmtp.quit())
		return (response.code, response.message)

	def ehlo(self):
		response = run_coroutine_sync(self._asmtp.ehlo())
		return (response.code, response.message)

	def has_extn(self, name):
		return self._asmtp.supports_extension(name)

	@property
	def esmtp_features(self):
		return self._asmtp.esmtp_extensions
