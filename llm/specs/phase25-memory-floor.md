# Phase 25: Memory floor — boot-preload diet + libsql churn fix

**Part:** Cleanup / Memory (follow-up to Phase 24)
**Depends on:** Phase 24 complete. Driven by the memray + tracemalloc deep-dive
(improvement.md 2026-06-08 00:20).
**Usable after:** yes — every sub-phase independently shippable.

## Goal

Lean-site PSS idle 142 → ~110-120 MB, working-set settle 236 → ~200 MB, by
removing the remaining boot fat and the libsql allocation churn that sets the
allocator high-water. Realistic floor after this phase ≈ 105-120 idle; hard
floor ≈ ~95 (interpreter 12-15 + trimmed module code 25-30 + framework objects
35-40 + C side/thread stacks 10-15). Below that = framework surgery (lazy meta,
-OO, module pruning) — out of scope, diminishing returns.

Small sites should also run the **GIL build** (free-threading costs ~+10 MB for
parallelism a small site doesn't use — measured on bench-tmp) and **glibc**
(see 25.2).

## Findings being fixed (profiled, not guessed)

1. `frappe/app.py:31-60` gunicorn-era preload block ("import before fork" —
   obsolete, single process) force-imports rq+redis (via background_jobs +
   redis_wrapper), mysqlclient, bs4+html5lib (via website_generator →
   website_search), requests+urllib3+charset_normalizer (via boot →
   changelog_feed), num2words, babel.messages — on every serving process.
   Defeats 24.1 (whose import-budget test only probes `import frappe`).
2. libsql decltype map (`libsql_compat.py:304`): ANY unknown result column —
   including every alias/expression (`count(name)`, `sum(x) AS total`) —
   triggers a FULL sqlite_master × pragma_table_info rescan (memray: 49-135 MB
   cumulative churn). Plus `_names()` allocates fresh lists per row, twice.
3. The 30s trim tick calls `malloc_trim(0)` — glibc API, silently ignored by
   tcmalloc → tcmalloc retains ~50 MB more after load (285 vs 236 settle).

# Sub-phases

## 25.0 — DONE — Boot-preload diet (drop ONLY waste; hot preloads stay)

User direction: keep hot preloads, remove only the absolutely unnecessary.
- `frappe/app.py` preload block, drop exactly 5 lines:
  `frappe.utils.background_jobs`, `frappe.utils.redis_wrapper`,
  `frappe.database.mariadb.mysqlclient` (preload_configured_backends warms the
  configured ones at lifespan), `num2words` (optional feature), `babel.messages`
  (translation compile tooling). Plain `babel` and every other line stays.
- Break heavy transitive imports while keeping the modules preloaded:
  - `search/website_search.py:6` `from bs4 import BeautifulSoup` → function-level
  - `desk/notifications.py:7` same
  - `boot.py:16` changelog_feed import → lazy inside the calling function
    (kills requests/urllib3/charset_normalizer at boot)
- Extend `tests/test_import_budget.py`: second fresh-subprocess probe importing
  `frappe.app` with the same STRICT denylist + own module budget — pins the fix.

## 25.1 — DONE — libsql decltype churn fix

`database/sqlite/libsql_compat.py`:
- `_column_decltypes`: full rebuild ONLY when an unknown name is a plain
  identifier (`[A-Za-z_][A-Za-z0-9_]*`); aliases/expressions → straight
  `setdefault(name, None)`, no rescan. Real ALTER TABLE columns still rescan.
- Per-cursor cache of the names list + decltypes list (description stable per
  statement) — kills 2-3 list allocations per row.
- Module counter `_decltype_rebuilds` exposed in `frappe.utils.memstats`.
- Tests: test_libsql_compat stays green; new test asserts alias query does NOT
  bump the counter, new real column DOES.

## 25.2 — DONE — Lean allocator default (bench-root app.py)

glibc+trim beats tcmalloc on lean sites (idle 148 vs 154, settle 236 vs 285;
trim is a no-op under tcmalloc). Same pattern as 24.10's pool floor: lean
backend set active AND `use_tcmalloc` unset → default 0. Heavy benches keep
tcmalloc default; knob overrides everywhere.

## 25.3 — DONE — Re-measure + docs

- memprofile import + new frappe.app probe, tag `25-final`.
- bench-tmp lean-server sequence (true idle → warmup+freeze → 30s autocannon →
  settle); expect idle ~110-120 PSS, modules ~800, settle ~200.
- Before/after table in improvement.md; GIL-build note for small sites.

## Verification

- unit category green; import_budget (both probes), libsql_compat (+1),
  sqlite_queue, rq_job_sqlite green.
- Fresh `import frappe.app`: no rq/redis/bs4/requests.
- bench-tmp: homepage 200, rps ±5% of 52, PSS reduced.
- Heavy sanity: mariadb test.local serves; rps parity.

## Rollback

Each sub-phase one commit, mechanical inversions. Preload drops are 5 deleted
lines; lazy moves are import relocations; libsql gate is one condition + cache
fields; allocator default is one bench app.py line.
