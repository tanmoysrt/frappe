# Phase 21: Cleanup — Remove Redis cache dependency

**Part:** Cleanup / Dependency removal
**Depends on:** Phase 18. Do Phase 19 (common-config options) first — the backend switch is a common-config knob.
**Usable after:** yes — redis stays as opt-in backend for multi-process deployments.

## Goal

Light mode (`python3 app.py`) boots with ZERO redis processes. One process =
the cache can live in that process's memory. Redis cache becomes an opt-in
backend (`cache_backend: "redis"`) for heavy/multi-process deployments, same
pattern as queue_backend (Phase 10).

## Current state (audited 2026-06-07)

- `frappe.cache` = `RedisWrapper(redis.Redis)` subclass (redis_wrapper.py:56) —
  386 call sites use it as a real redis client with `make_key` prefixing.
- `frappe.cache.aio` = AsyncRedisWrapper (Phase 3, dual stack).
- `ClientCache` (redis_wrapper.py:862) = in-process dict layer over redis with
  redis-driven invalidation — holds doctype_meta etc.
- Cache redis (13001) is currently MANDATORY for boot: sessions, boot info,
  `frappe.cache.sismember` in desk.py path (the SessionBootFailed the user hit).
- Sessions: cache is acceleration; durable copy in `tabSessions` — verify
  resume-from-DB path works cache-cold before relying on it.
- Queue redis (11001) already optional (Phase 10/14) — NOT this phase's scope.

## Design

`InProcessCache` — a frappe-native class implementing the redis command
subset frappe actually uses, over plain dicts + a single lock (Phase 23
free-threading makes the lock load-bearing, design it in now):

- Audit first: `grep -rn "frappe\.cache\(\.aio\)\?\.[a-z_]*("` → frequency
  table of commands. Expected: get/set/setex/delete/exists/expire/ttl,
  hget/hset/hgetall/hdel/hexists, sadd/srem/sismember/smembers,
  incrby/decrby, keys/delete_keys (prefix scan), lpush/rpop?, pipelines
  (rate_limiter), pubsub (ClientCache invalidation — becomes a no-op
  in-process, the dict IS the client cache).
- TTL: lazy expiry on read + periodic sweep coroutine on the lifespan loop
  (reuse the malloc-trim cadence pattern).
- Memory bound: total-entry cap with FIFO/LRU eviction + `FRAPPE_CACHE_MAX_MB`
  guidance; expose stats via an admin endpoint for the RSS investigations.
- `frappe.cache.aio`: same object behind an awaitable facade — in-process ops
  are non-blocking, the facade is just `async def` returning directly (no
  pool hop, no bridge — faster than redis ever was).
- `ClientCache` collapses to a thin alias over InProcessCache when backend is
  memory (one dict, no invalidation protocol needed).

## The hard problem: cross-process invalidation

bench CLI (`bench clear-cache`, migrations) runs in a SEPARATE process and
today invalidates the web process via shared redis. With in-memory cache:

- Chosen design: **bump-file + lazy check.** CLI writes a monotonic value to
  `sites/.cache-generation` (atomic rename); web process checks mtime at most
  once per second (cached stat, amortized to ~0 cost) and flushes on change.
  Restart-safe, no ports, no auth surface.
- Rejected: HTTP admin endpoint (auth surface + fails when server down —
  migrations run with server stopped anyway, where stale cache is moot);
  keeping redis just for pubsub (defeats the phase).
- RQ workers in heavy mode need shared cache → heavy mode keeps
  `cache_backend: "redis"`. Document the rule: multi-process ⇒ redis.

## Tasks

1. Command-usage audit + InProcessCache with exactly that subset
   (`frappe/utils/inprocess_cache.py`), unit tests mirroring redis semantics
   (TTL, type errors, prefix scan).
2. Backend switch in `setup_redis_cache_connection()` (frappe/__init__.py:320):
   `cache_backend` from COMMON site config, default **"memory"**;
   "redis" keeps today's wiring untouched.
3. `.cache-generation` bump-file: write in clear_cache + migrate paths, lazy
   check in web process.
4. Session cache-cold path verified; fix if resume-from-tabSessions broken.
5. Make `get_socketio_secret` / other `get_redis_connection_without_auth`
   cache-adjacent strays backend-aware or DB-backed.
6. Procfile: comment `redis_cache:` line (rollback pattern); boot smoke test
   with NO redis running at all (cache memory + queue sqlite + realtime direct).
7. improvement.md: RSS delta (redis-server RSS ~5-15 MB leaves the stack;
   in-process dict adds back inside the python process — report both, net win
   expected small but the ops win is the headline: zero external deps).

## Risks

- Semantics drift vs redis (binary vs str keys, pickle layer in RedisWrapper) —
  RedisWrapper already pickles values; keep identical encode/decode so backend
  swap is invisible.
- Some code may bypass the wrapper and grab raw redis conns
  (`get_redis_connection_without_auth` in realtime/socketio secret) — audit gate:
  grep for direct `redis.` usage outside redis_wrapper/queue backends.
- Cache no longer survives restart (redis did). Acceptable: cache is cache;
  meta/session warmup is the existing cold-boot path.

## Verification

- Unit green with `cache_backend: memory`; full run with redis STOPPED.
- `bench clear-cache` from CLI invalidates running server (bump-file e2e).
- Benchmark: expect rps gain (no 13001 round-trips on hot paths) — record.
- Heavy-mode regression: `cache_backend: redis` run stays green.

## Rollback

`cache_backend: "redis"` in common config + uncomment Procfile line.
