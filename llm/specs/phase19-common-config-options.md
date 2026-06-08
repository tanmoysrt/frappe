# Phase 19: Cleanup — Migration options live in common site config

**Part:** Cleanup / Configuration
**Depends on:** Phase 18. Do this BEFORE phases 20/21 — they add knobs and need the convention settled.
**Usable after:** yes.

## Goal

Every architecture-level option the migration introduced is configured ONCE
in `sites/common_site_config.json` — not per site, not env vars. These choose
the process architecture (async DB driver, queue backend, realtime mode);
a bench runs ONE process shape, so per-site values are wrong by construction
(e.g. `use_async_db` on for test.local but off for site2 = two driver stacks
in one process for no reason).

## Current state (audited 2026-06-07)

Per-site or env today, should be common:

| Option | Today | Read sites |
|---|---|---|
| `use_async_db` | site_config (test.local, sqlite-async.local) | database/__init__.py, mariadb/aio.py, sqlite/aio.py |
| `queue_backend` | conf (merged, set per-site in tests) | background_jobs.py, sqlite_queue.py, arq_queue.py, safe_exec.py |
| `use_node_realtime` | conf | realtime.py:114 |
| `socketio_port` | common (already) | boot.py, realtime |
| `db_pool_size` | conf | mariadb/aio.py |
| `in_process_scheduler` | conf (revert knob) | scheduler.py |
| `FRAPPE_POOL_SIZE`, `FRAPPE_LIMIT_CONCURRENCY`, `FRAPPE_PORT`, `FRAPPE_THREAD_STACK`, `FRAPPE_MALLOC_TRIM_INTERVAL`, `FRAPPE_USE_TCMALLOC` | env vars (bench-root app.py) | app.py |

Stays per-site (correctly site-scoped): `enable_scheduler`, `allow_tests`,
`developer_mode`, db credentials.

Note: `frappe.conf` already merges common + site config with site winning —
the problem is the override direction. Architecture knobs must NOT be
site-overridable.

## Design

- New helper `frappe.get_common_conf(key, default=None)` (or extend
  `frappe.get_common_site_config()` usage) — reads ONLY
  common_site_config.json, never the site config. Cached per process,
  invalidated with the Phase 21 bump-file.
- It works WITHOUT site init (app.py boot needs pool size before any
  `frappe.init`) — `frappe.get_common_site_config(sites_path)` already does;
  app.py drops its env-var parsing and reads common config (env vars remain
  as LAST-resort override for container deployments, documented order:
  env > common config > default).

## Tasks

1. Add `get_common_conf` helper + tests (missing file, malformed json, no-site context).
2. Flip read sites for the table above from `frappe.conf.X` /
   `frappe.get_conf().get(X)` to `get_common_conf(X)`. One commit per option
   group (db / queue / realtime / serve knobs) — small diffs, greppable.
3. app.py: env knobs become common-config keys (`asgi_pool_size`,
   `asgi_limit_concurrency`, `webserver_port` (exists already), `malloc_trim_interval`,
   `use_tcmalloc`), env override preserved.
4. Migrate live values: move `use_async_db: 1` out of test.local +
   sqlite-async.local site_config.json into common_site_config.json; delete
   per-site keys. Same for any queue/realtime keys that drifted per-site.
5. Tests that pin options per-site (test_rq_job setUp pins queue_backend etc.)
   switch to patching the common-conf reader, not site config.
6. Docs: table of all architecture knobs + their home, in specs/ or
   improvement.md.

## Risks

- Sites used `frappe.conf` merge semantics implicitly — a per-site
  `use_async_db: 0` silently stops working after the flip. Acceptable and
  intended; log a warning at boot when a site config contains an
  architecture key that is now ignored.
- Test isolation: common config is bench-global — tests mutating it must
  restore (context manager `patch_common_conf`, mirror existing
  `change_settings` pattern).

## Verification

- Unit + integration green; rq/arq/sqlite queue tests green with patched reader.
- Boot with use_async_db ONLY in common config — both sites get async driver.
- Boot warning fires for leftover per-site architecture keys.
- No benchmark delta expected (config reads are boot-time/cached) — verify flat.

## Rollback

Reads are mechanical (`get_common_conf(X)` ↔ `conf.X`) — revert commits
individually; per-site values restorable from git-less site_config backups
(copy before migrating, note path in improvement.md).
