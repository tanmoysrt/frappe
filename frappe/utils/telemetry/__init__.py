"""Basic telemetry for improving apps.

WARNING: Everything in this file should be treated "internal" and is subjected to change or get
removed without any warning.
"""

import frappe
from frappe.utils import getdate
from frappe.utils.caching import site_cache

# posthog provider: imported lazily (PEP 562 + function-level) — the posthog
# package top-imports requests (urllib3 + charset_normalizer), which otherwise
# rides into every boot via frappe.api / desk.form.save (Phase 25.0).

# pulse provider
from .pulse.client import capture as pulse_capture
from .pulse.client import is_enabled as is_pulse_enabled

_POSTHOG_ATTRS = {
	"POSTHOG_HOST_FIELD": "POSTHOG_HOST_FIELD",
	"POSTHOG_PROJECT_FIELD": "POSTHOG_PROJECT_FIELD",
	"ph_capture": "capture",
	"is_posthog_enabled": "is_enabled",
	# backward-compatible alias
	"init_telemetry": "init_telemetry",
}


def __getattr__(name):
	target = _POSTHOG_ATTRS.get(name)
	if target is None:
		raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
	from . import posthog

	value = getattr(posthog, target)
	globals()[name] = value
	return value


def add_bootinfo(bootinfo):
	from .posthog import POSTHOG_HOST_FIELD, POSTHOG_PROJECT_FIELD
	from .posthog import is_enabled as is_posthog_enabled

	bootinfo.telemetry_site_age = site_age()
	bootinfo.telemetry_provider = []

	if is_posthog_enabled():
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

	if is_posthog_enabled():
		ph_capture(event, app, **kwargs)

	if is_pulse_enabled():
		pulse_capture(event, app=app, **kwargs)


def capture_doc(*args, **kwargs):
	# lazy wrapper: posthog (→requests) imported only when telemetry actually fires
	from .posthog import capture_doc as _ph_capture_doc

	return _ph_capture_doc(*args, **kwargs)


@site_cache(ttl=60 * 60 * 12)
def site_age():
	try:
		est_creation = frappe.db.get_value("User", "Administrator", "creation")
		return (getdate() - getdate(est_creation)).days + 1
	except Exception:
		pass
