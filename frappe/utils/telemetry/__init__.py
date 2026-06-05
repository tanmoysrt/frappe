"""Basic telemetry for improving apps.

WARNING: Everything in this file should be treated "internal" and is subjected to change or get
removed without any warning.
"""

import frappe
from frappe.utils.caching import site_cache


def add_bootinfo(bootinfo):
	from .posthog import is_enabled as is_posthog_enabled
	from .pulse.client import is_enabled as is_pulse_enabled

	bootinfo.telemetry_site_age = site_age()
	bootinfo.telemetry_provider = []

	if is_posthog_enabled():
		from .posthog import POSTHOG_HOST_FIELD, POSTHOG_PROJECT_FIELD

		bootinfo.enable_telemetry = True
		bootinfo.telemetry_provider.append("posthog")
		bootinfo.posthog_host = frappe.conf.get(POSTHOG_HOST_FIELD)
		bootinfo.posthog_project_id = frappe.conf.get(POSTHOG_PROJECT_FIELD)

	if is_pulse_enabled():
		bootinfo.enable_telemetry = True
		bootinfo.telemetry_provider.append("pulse")


def capture(event, app, **kwargs):
	from .posthog import capture as ph_capture
	from .posthog import is_enabled as is_posthog_enabled
	from .pulse.client import capture as pulse_capture
	from .pulse.client import is_enabled as is_pulse_enabled

	if is_posthog_enabled():
		ph_capture(event, app, **kwargs)

	if is_pulse_enabled():
		pulse_capture(event, app=app, **kwargs)


@site_cache(ttl=60 * 60 * 12)
def site_age():
	from frappe.utils import getdate

	try:
		est_creation = frappe.db.get_value("User", "Administrator", "creation")
		return (getdate() - getdate(est_creation)).days + 1
	except Exception:
		pass


def __getattr__(name):
	# for backward compatibility, avoids importing providers at module load
	if name == "init_telemetry":
		from .posthog import init_telemetry

		return init_telemetry
	if name == "capture_doc":
		from .posthog import capture_doc

		return capture_doc
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
