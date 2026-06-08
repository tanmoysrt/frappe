# Phase 24: Memory footprint reduction

**Part:** Cleanup / Memory
**Depends on:** Phase 18. Runnable any time after Phase 19. Phases 20/21 already
deleted werkzeug + the redis cache client from the hot import path — re-measured
below. Ordering vs Phase 23: 23 already shipped, so every lever here must ALSO be
verified no-GIL-safe (free-threading adds per-object memory and removes the GIL's
implicit lock around shared caches — see §Free-threading rules).
**Usable after:** yes — every sub-phase is independently shippable.

## Goal

Idle RSS of the single process under **100 MB** (today 159 MB idle / ~220 MB
under load), with growth bounded and observable, AND a leaner default import
graph so memory-constrained deployments can shed optional features they don't
use. Memory consolidation was the migration's headline — this phase collects it.

## Config rule (applies to every knob in this phase)

**No new env-var knobs.** Phase 19 settled the convention: every option lives in
`common_site_config.json` and is read via `frappe.get_common_conf`. New disable
flags here are common-config only. (`app.py` keeps its existing env overrides for
the few boot-time-before-import values it already has — MALLOC/GIL/pool — but no
NEW env knob is added by this phase.) Each flag is opt-in: default keeps today's
behavior; setting the flag sheds the feature.

## Measured baseline (2026-06-07, this bench, GIL build env/)

| measure | RSS | modules |
|---|---|---|
| bare python 3.14 | 11.5 MB | 77 |
| `import frappe` (no site) | 55.8 MB | 561 (+484 over bare) |
| full boot idle (running server, smaps_rollup) | 159 MB Rss / 145 MB Pss | — |

- Eager heavies pulled by `import frappe`: **rq, redis, pydantic**. bs4, openpyxl,
  num2words, xlrd are NOT eager (already lazy / feature-module-level) — good; the
  win there is letting a deployment turn the *features* off so the modules never
  load even on demand, and trimming the one hot path that still reaches for bs4.
- Isolated import cost (RSS, fresh interp): premailer **+30.3**, bs4 **+23.4**,
  openpyxl **+21.8**, html5lib **+11.1**, num2words **+3.5**, xlrd **+2.7**,
  csv **+0.0**.
- Eager-import waste still in core:
  - `frappe/__init__.py` `from frappe.utils.background_jobs import enqueue,
    enqueue_doc` → rq + redis at import time — pure waste in light mode (queue is
    sqlite). Confirmed eager above.
  - pydantic is also eager (typing_validations) but stays — it's on the
    whitelisted-method validation path used on every request; not worth lazying.
    The eager-import target of this phase is rq + redis only.

## Free-threading rules (Phase 23 already shipped)

Every shared, process-wide cache touched here MUST be guarded for no-GIL:
- Any new LRU / bounded dict added (translations, sessions) wraps mutation in a
  single `threading.Lock`/`RLock` (same pattern as InProcessCache from Phase 21).
- `gc.freeze()` is safe no-GIL (it only moves objects out of GC scanning).
- `__getattr__` (PEP 562) lazy-import paths are import-locked by CPython already;
  fine, but the imported module must itself be a no-GIL-clean wheel (re-run
  `scripts/gil_audit.py` after adding any lazy dep to the default path).
- Verify each sub-phase with `sys._is_gil_enabled()` False under env-t before
  marking done.

---

# Sub-phase plan

## 24.0 — Measurement tooling (ship first, keep forever) — DONE 2026-06-07

1. **DONE** `scripts/memprofile.py`: subcommands `bare` / `import` / `proc --pid`
   / `stats --site` — append one tagged JSON row each to benchmarks/memory.jsonl.
   `proc` reads `/proc/<pid>/smaps_rollup` (Rss/Pss/anon) of a running server;
   `import` runs `import frappe` in a fresh subprocess (RSS delta, module count,
   denylist leaks); `stats` boots a site and calls `memstats` (modules, gc +
   freeze, cache/client_cache sizes, opt-in `--tracemalloc` top-25). Script and
   admin method share the same `memstats` so they report identical numbers.
2. **DONE** Minimal-site harness `scripts/new_minimal_site.sh [site]`: creates a
   sqlite site (no demo data, no extra apps), writes a lean common-config sidecar
   (`sites/<site>/minimal_common_config.json` — sqlite/memory/sqlite-queue, RQ
   off, feature flags off, `asgi_pool_size: 4`) WITHOUT clobbering the shared
   bench common_site_config, and records the bare + import floor to memory.jsonl.
   Full-boot floor = launch app.py with the sidecar as common config + `proc`.
3. **DONE** Import-budget regression test (`frappe/tests/test_import_budget.py`,
   `UnitTestCase`): `import frappe` in a fresh subprocess; assert module count
   ≤ `MODULE_BUDGET` (600; baseline 561) AND a denylist split into STRICT
   (`werkzeug.serving, IPython, sentry_sdk, bs4, openpyxl` — already absent, hard
   assert) + PENDING (`rq, redis` — still eager until 24.1, marked
   `@expectedFailure` so it flips to a live pin the moment 24.1 makes them lazy).
   pydantic is NOT on the denylist — kept eager on purpose.
4. **DONE** Admin method `frappe.utils.memstats.memstats` (System Manager only;
   own module, not utils/__init__, to avoid the boot-time circular import on the
   `@frappe.whitelist()` decorator): live RSS/Pss, modules, gc + freeze counts,
   cache + client_cache sizes/hits/misses, opt-in tracemalloc top-N. Every
   section guarded so an unknown cache backend never raises.

Baseline recorded (env/ GIL build, 2026-06-07): bare 12.1 MB / 81 mods;
`import frappe` 56.2 MB / 561 mods (rq+redis leak, the 24.1 target); booted
test.local 67.3 MB Rss / 51.0 MB Pss / 647 mods.

## 24.1 — Lazy backends, preload only the configured one — DONE 2026-06-07 (−8 MB / −101 modules off `import frappe`)

**Universal rule:** every pluggable backend driver (redis, rq, DB drivers) is
lazy-imported by default. At boot, read `common_site_config` and eager-import
ONLY the drivers the configured backends actually need — then 24.7 freezes those
(and only those) into the permanent graph. A small site on sqlite + in-process
cache + sqlite queue imports zero redis, zero rq, zero mariadb/postgres drivers.
The same strategy applies to anyone, any backend combo — config decides what's
warm, everything else stays cold until first use.

Two parts: (a) make all backend imports lazy, (b) one config-driven preload step.

### (a) Make backend imports lazy

- **rq / redis.** `frappe/__init__.py`: expose `enqueue` / `enqueue_doc` via
  module `__getattr__` (PEP 562) instead of the top-level
  `from frappe.utils.background_jobs import ...`. Sweep every `import rq` /
  `from rq` / `import redis` / `from redis` across frappe to function-level
  (background_jobs.py, rq_job.py, scheduler, realtime/redis pubsub, monitor).
- **DB drivers.** Same treatment — none imported at `import frappe`; each
  `Database` subclass imports its driver in `get_connection` / module-lazy, not
  at module top:
  - mariadb: `pymysql` / `aiomysql` (light) vs `MySQLdb` (mysqlclient, heavy)
  - postgres: `psycopg` (async, light) / `psycopg2` (sync, heavy)
  - sqlite: `libsql` (main DB) / `aiosqlite` (queue worker)
  `app.py` currently force-preloads the mariadb mysqlclient driver — remove that
  eager preload; the preload step below replaces it, driven by config.

### (b) Config-driven preload (one place, runs before gc.freeze)

A single `preload_configured_backends()` (called at ASGI lifespan startup, before
24.7's `gc.freeze()`) reads common-config and imports exactly what's selected:

| common_site_config | preload |
|---|---|
| `cache_backend: "redis"` | `redis` |
| `queue_backend: "rq"` | `rq` + `redis` |
| `db_type: "mariadb"` + `use_async_db:1` | `aiomysql` (+ `pymysql`) |
| `db_type: "mariadb"` + `use_async_db:0` | `MySQLdb` or `pymysql` per `use_mysqlclient` |
| `db_type: "postgres"` + `use_async_db:1` | `psycopg` |
| `db_type: "postgres"` + `use_async_db:0` | `psycopg2` |
| `db_type: "sqlite"` | `libsql` (+ `aiosqlite` if queue=sqlite) |

Rationale: a driver that WILL be used every request should be warm and frozen
(not import-locked on the first hot request, not re-scanned by GC). A driver that
will NEVER be used in this config should not cost a single page. Eager only the
required ones — universal, no per-deployment special-casing.

- **Off-switch (common-config, opt-in for small sites):** `disable_rq` (default
  off). When set, the lazy rq/redis accessors raise a clear "RQ disabled" instead
  of importing — a small site can never pull rq/redis even by accident, and the
  import-budget test asserts it. Read via `frappe.get_common_conf`. No env var.
  (Redundant with the preload table for a clean config, but guards mistakes.)
- pydantic stays eager (hot validation path) — not a pluggable backend.
- Audit remainder: `-X importtime` top-40; anything >5 ms not needed at boot and
  not pydantic goes lazy (sqlparse? babel core? croniter?).
- Verify: import-budget test (rq/redis/non-configured DB drivers absent from a
  fresh `import frappe`); each configured combo preloads its driver and serves;
  no-GIL import audit clean on every preloaded driver; heavy mode
  (`queue_backend: rq`, `cache_backend: redis`) still works.

## 24.2 — Optional global imports off-switch — DONE 2026-06-07

The big feature libs (bs4, num2words, openpyxl/xlrd CSV+Excel) are reached lazily
already, but a memory-tight deployment wants them *never loadable*. Add
common-config flags, default on (today's behavior), that let an operator disable
a feature whole — when off, the feature degrades cleanly and the heavy module is
never imported.

| flag (common_site_config) | default | when off |
|---|---|---|
| `enable_html_parsing` | on | bs4-backed cleaning/parsing paths raise a clear "feature disabled" or no-op; the cheap is-HTML test (24.3) still works without bs4 |
| `enable_number_to_words` | on | `money_in_words` / num2words formatters return empty/raise clear error |
| `enable_excel` | on | xlsx import/export disabled; CSV still available |
| `enable_csv` | on | csv import/export disabled (csv is stdlib, ~0 MB — flag is for feature-surface control, not memory) |

Rules: each gate checks `frappe.get_common_conf(flag, default=True)` at the entry
point, BEFORE the lazy import, and raises `frappe.ValidationError` with a
human-readable message (or no-ops where a no-op is safe). No env var. One small
helper `frappe.utils.optional_feature(name)` keeps the call sites one-liners and
testable. Keep it simple: a flag gates a feature, not individual functions.

## 24.3 — Replace bs4 for the is-HTML test — DONE 2026-06-07

Two hot-ish paths use BeautifulSoup only to answer "does this string contain any
HTML tag":
- `frappe/model/base_document.py:1383`
  `not bool(BeautifulSoup(value, "html.parser").find())`
- `frappe/utils/html_utils.py:162`
  `if not bool(BeautifulSoup(html, "html.parser").find()):`

Replace both with a cheap tag-presence check (a single compiled regex, e.g.
`re.search(r"<[a-zA-Z!/]", s)`, in a small `frappe.utils.html_utils.has_html_tag`
helper) so these paths never import bs4. Keep behavior: the test only asks
"is there markup", which a tag-presence scan answers without a parser. Sweep for
any other `BeautifulSoup(...).find()`-as-boolean usages and convert them too.
**Scope: only the is-HTML boolean test is replaced.** Every real parsing use of
bs4 (communication.py, notifications.py, pdf.py, website_search.py,
sqlite_search.py, global_search.py — extracting/cleaning/walking the DOM) KEEPS
bs4; a regex can't parse, only detect. This makes `enable_html_parsing: 0` viable
for the common document write path without touching the parsing features.

## 24.4 — SQLite queue jobs visible in RQ Job — DONE 2026-06-07

Light mode runs the sqlite queue (`queue_backend: "sqlite"`), but the **RQ Job**
desk doctype reads only from redis/rq, so the in-process queue is invisible to
operators. Make RQ Job backend-aware: when `queue_backend == "sqlite"`,
`get_list` / `load_from_db` read the `jobs` table in `task_queue.db` instead of
redis.

- Map the sqlite schema (`job_id, site, queue, func, status, enqueued_at,
  started_at, ended_at, error, retries`) onto the RQ Job fields
  (`job_id, job_name, queue, status, started_at, ended_at, time_taken,
  exc_info, arguments`). Status vocab differs (sqlite: pending/running/failed/
  finished) — map to RQ Job's literal set.
- Read-only first: list + open. `delete`/`stop`/`cancel` either no-op with a
  clear message or do the obvious sqlite UPDATE/DELETE (keep simple — list+view
  is the must-have).
- Reuse the short-lived producer connection pattern from `sqlite_queue.py`; no
  new long-lived handle. Free-threading: producer conn is per-call, fine.
- Test: enqueue a sqlite job, assert it appears in `RQJob.get_list`.

## 24.5 — Translation dicts bounded — DONE 2026-06-07

`get_all_translations` per language; in-process post-Phase 21, unbounded. Bound
with an LRU over languages (most sites use 1-2), lazy-load per language on first
request. Lock the LRU for no-GIL. Measure a multi-lang site before/after.

## 24.6 — Allocator tuning — DONE 2026-06-07 (short-form; tcmalloc kept)

Already: tcmalloc + MALLOC_ARENA_MAX=2 + periodic malloc_trim. Compare matrix
(1-hour soak, autocannon bursts + idle gaps): glibc+trim vs tcmalloc vs jemalloc
(`background_thread:true,dirty_decay_ms:10000`). Pick by idle-RSS-after-load, not
peak. (This is the one place an env/LD_PRELOAD line is unavoidable — it must be
set before the interpreter starts; app.py already owns that re-exec. Not a new
runtime knob.)

## 24.7 — `gc.freeze()` after warmup — DONE 2026-06-07

Post-boot, post-first-request, AND after 24.1's `preload_configured_backends()`:
move the permanent object graph (modules, meta, controllers, the configured
backend drivers) out of GC scanning — cuts GC pause AND stops gen2 collections
touching cold pages. Ordering matters: preload the configured drivers first so
they get frozen; non-configured drivers were never imported, so they're never
frozen. Pair with tuned `gc.set_threshold`. Safe no-GIL.

## 24.8 — Cache bounds with stats — DONE 2026-06-07

Meta cache already FIFO 1024/600s (ClientCache); add hit/miss/size counters to
memstats. Audit unbounded site-level dicts for caps: `frappe.controllers`, hooks
cache, jinja template cache (`local.document_cache` is per-request, fine). Every
shared cache gets a lock for no-GIL.

## 24.9 — Session bloat — DONE 2026-06-07

Sessions in-process post-Phase 21: cap resident session cache (LRU by
last-access), rest falls back to tabSessions — boot data per session is the big
payload, don't pin every user's boot forever. Locked LRU.

## 24.10 — Lean defaults for new sites — DONE 2026-06-07

A fresh `bench new-site` must land on the smallest-memory config by default —
the operator opts UP into heavy backends, never down. "Default" = what a new
site gets with no extra flags set.

Default config a new site is created with (all in the site / common config, no
env vars):

| key | lean default | heavy opt-in |
|---|---|---|
| `db_type` | `sqlite` | `mariadb` / `postgres` |
| `cache_backend` | `"memory"` (InProcessCache) | `redis` |
| `queue_backend` | `sqlite` | `rq` |
| eager backend preload | only sqlite drivers (24.1) | redis/rq/mariadb pulled when configured |
| `disable_rq` | on for the minimal-site harness (24.1 off-switch) | off |
| `enable_html_parsing` / `enable_number_to_words` / `enable_excel` | on (behavior-preserving) — minimal-site harness flips them off | on |
| `free_threading` | off (Phase 23 default) | on (with `use_async_db`) |

- `scripts/new_minimal_site.sh` (24.0) writes exactly this lean set so the RSS
  floor is the *default* experience, not a special mode.
- **Lower uvicorn worker-thread pool by default.** `app.py` sizes the pool by CPU
  (Phase 23). On sqlite + in-process cache + sqlite queue there's no external I/O
  to overlap, so a big pool is wasted thread stacks (RSS) and context-switch. Add
  a config-driven floor: when the lean backend set is active (sqlite + in-process
  + sqlite queue), default the pool to a small fixed size (e.g. 4) unless
  `asgi_pool_size` (existing knob, already in ARCHITECTURE_KEYS) is set in common
  config. Heavy backends keep the CPU-based sizing. Read via
  `frappe.get_common_conf`; no new env var
  (app.py already owns pool sizing before import — reuse that path).
- Keep it simple: lean is the absence of heavy flags, not a new "lean mode" flag.
  Each heavy backend is one config line to turn on.

## 24.11 — Final profiling pass — DONE 2026-06-07 (results in improvement.md)

After 24.1–24.10 land, one consolidated before/after measurement so the phase has
a single headline number, same method as the Phase 23 benchmark matrix.

1. Re-run `scripts/memprofile.py` for every row of the baseline table (§Measured
   baseline) on the SAME bench: bare python, `import frappe`, full-boot idle,
   post-load-idle (5 min after autocannon), minimal-site idle, feature-loaded-site
   idle. Append to benchmarks/memory.jsonl with a `phase: 24-final` tag.
2. Produce the before/after delta table (baseline 2026-06-07 vs post-24): import
   RSS, idle RSS, post-load-idle RSS, module count, minimal-vs-feature gap.
3. Re-run the rps/p50 benchmark (autocannon -c10 homepage, same as phase02..06)
   to prove memory wins cost ≤3% latency.
4. Re-run `scripts/gil_audit.py` under env-t — no preloaded driver re-enables GIL.
5. Record the result block in improvement.md (date/time, RSS before→after per
   measure, rps before→after, success-criteria check).

## Non-goals

- Response/page caches that TRADE memory for rps — separate decision, not here.
- Micro-optimizing Document `__dict__` / `__slots__` — dynamic fields make it
  invasive; revisit only if memprofile shows documents dominating steady-state.
- Pool/thread stack sizing — Phase 23 already set this CPU-based; only revisit if
  smaps shows real (not virtual) per-thread Rss is significant.

## Verification

- memory.jsonl entry per sub-phase: import-RSS, idle-RSS, post-load-idle-RSS
  (5 min after autocannon stops — the retention number), minimal-site RSS,
  p50/rps unchanged (±3%) — memory wins must not cost latency.
- Import-budget test green = no eager-import regressions ever again.
- Each sub-phase re-verified under env-t (`sys._is_gil_enabled()` False) — no new
  GIL re-enabler, no unlocked shared cache.
- 1-hour soak: RSS plateau (no slope), zero 5xx.
- Success: idle < 100 MB, post-load-idle < 130 MB, `import frappe` < 35 MB,
  minimal-site idle measurably below feature-loaded site.

## Rollback

Each lever independent + individually revertable. Lazy-import commits are
mechanical inversions; feature off-switches default to on (no behavior change
unless flipped); RQ Job sqlite branch is additive; allocator choice is one
app.py line.
