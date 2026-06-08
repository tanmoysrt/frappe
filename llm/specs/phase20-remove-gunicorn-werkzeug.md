# Phase 20: Cleanup — Remove gunicorn + Werkzeug (async-first request path)

**Part:** Cleanup / Core inversion
**Depends on:** Phase 18 (migration complete). Do Phase 19 (common-config options) first — this phase adds knobs.
**Usable after:** yes — each sub-step keeps the framework serving; Werkzeug is deleted only in the final step.

## Goal

The framework becomes async-first: the native ASGI handler is the primary
request path, built directly on the ASGI scope — no WSGI environ, no
Werkzeug Request/Response, no gunicorn anywhere. Sync code keeps working
(pool-thread hop, as today) but is the slow path by design; we stop paying
the WSGI↔ASGI translation tax on every request.

## Current state (audited 2026-06-07)

- 32 files import `werkzeug` (`grep -rln werkzeug frappe --include='*.py'`).
- `frappe/asgi.py` still wraps the Werkzeug WSGI app (`application_with_statics()`)
  via scope→environ adapter + pool-thread call.
- gunicorn: pyproject git dep (frappe fork), used only by `bench serve` /
  production Procfiles elsewhere; this bench already serves via `app.py` → uvicorn.
- Werkzeug surfaces, by responsibility:
  1. **WSGI app + middlewares** — `frappe/app.py` (Request, Response, ClosingIterator,
     ProxyFix, ProfilerMiddleware, SharedDataMiddleware, `run_simple` dev server),
     `frappe/middlewares.py` (StaticDataMiddleware).
  2. **Request object** — `frappe.request` is a Werkzeug Request everywhere
     (auth.py, handler.py, api/v1, api/v2, oauth2, website/*). Form/file parsing =
     Werkzeug multipart parser. `frappe/utils/__init__.py` EnvironBuilder for
     `set_request` fake requests.
  3. **Response building** — `frappe/utils/response.py` (Response, `send_file`,
     `redirect`), website page renderers.
  4. **LocalProxy** — `frappe/utils/local.py` imports `werkzeug.local.LocalProxy`
     (the storage is already our ContextVar dict; only the proxy class is borrowed).
  5. **Exceptions** — `werkzeug.exceptions.HTTPException/NotFound/Forbidden`
     raised/caught in app.py, exceptions.py, path_resolver, response.py.
  6. **Test client** — `werkzeug.test.Client` (`frappe/utils/__init__.py:25`,
     test_api, test_cors, test_rate_limiter, …).

## Tasks (ordered sub-steps, one commit each)

1. **Drop gunicorn.** Remove pyproject git dep + `frappe/app.py serve()`'s
   gunicorn/run_simple paths; `bench serve` → programmatic uvicorn (reuse
   bench-root `app.py` logic, move it into `frappe/asgi.py:serve()` so the
   framework owns it). Procfile rollback line for old serve dies.
2. **Native static serving.** Replace SharedDataMiddleware(/assets) +
   StaticDataMiddleware(/files) with an ASGI static handler in asgi.py:
   aiofiles streaming, Range + ETag/Last-Modified + 304, path-traversal guard
   (resolve + prefix check). Private /files keep going through the app (permission
   check) as today.
3. **Native Request.** `frappe.request` becomes a frappe-native class built from
   the ASGI scope: attribute-compatible facade for the surface actually used
   (audit first: `grep -rn "request\.\(method\|path\|headers\|cookies\|args\|form\|files\|data\|host\|url\|full_path\|environ\|remote_addr\|scheme\|is_secure\|content_type\|query_string\|get_data\|max_content_length\)"`).
   Multipart/urlencoded parsing via `python-multipart` (new dep) on the pool
   thread. `set_request` builds it without EnvironBuilder. ProxyFix → ~15-line
   scope rewrite (X-Forwarded-For/Proto/Host) behind existing USE_PROXY knob.
4. **Native Response.** `frappe/utils/response.py` builds a frappe-native
   Response (status, headers, body | file | iterator); asgi.py sends it without
   the WSGI start_response capture. `send_file` → aiofiles + Range. `redirect`
   → 3-line Location response. ClosingIterator semantics become an explicit
   try/finally around the handler (frappe.destroy + after-request hooks BEFORE
   terminal send — keep the 5d03cf5bba ordering, it's load-bearing).
5. **Own LocalProxy + HTTP exceptions.** Vendor a minimal LocalProxy
   (~80 lines, `__getattr__`/`__setattr__`/dunder forwarding over the ContextVar
   dict) into frappe/utils/local.py. Replace werkzeug.exceptions usage with
   frappe-native `HTTPError(status_code)` hierarchy; frappe already routes on
   `http_status_code` — map NotFound→404 etc. at the asgi.py error boundary.
6. **Test client swap.** `frappe/utils/__init__.py` Client →
   `httpx.ASGITransport(app=frappe.asgi.application)` wrapper with the same
   call shape tests use; fix the ~8 test modules importing werkzeug.test.
   (Bonus: httpx transport closes properly — kills the known
   "Werkzeug client never closes the iterator" leak in tests.)
7. **Delete.** `Werkzeug==3.1.6` out of pyproject; `frappe/app.py` shrinks to
   the request-processing core that asgi.py calls (or merges into asgi.py);
   middlewares.py deleted. Grep gate: zero `werkzeug` imports outside
   deprecation_dumpster.

## Risks

- Biggest compat surface of any phase: every app touches `frappe.request`/
  `frappe.response`. Mitigation: attribute-compatible facades, audit-driven
  (only implement what grep finds used), one sub-step per commit, full
  integration run between steps.
- Multipart parser swap changes file-upload edge behavior (charset, max sizes —
  werkzeug's 500kb form limit was already overridden in app.py:70). Port the
  override; add upload tests before swapping.
- Werkzeug's Response does header encoding/cookie quoting subtleties (SameSite,
  Expires format). Steal its test vectors.

## Verification

- Full unit category green per sub-step; integration parity with known 294 pre-existing errors.
- Live: login → desk boot, file upload (small + >1 MiB spill), /assets and /files
  (Range request via curl -r), redirect flows (oauth2), websocket unaffected.
- Benchmark each sub-step (`scripts/benchmark.py phase --phase phase20-N`).
  Expect rps GAIN at step 4 (no environ build, no start_response bridge).
- RSS recorded in improvement.md.

## Rollback

Sub-steps 1–6 individually revertable (facades sit beside old code until 7).
After step 7 there is no Werkzeug rollback — same one-way contract as Phase 18.
