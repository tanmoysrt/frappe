# Phase 1: Single-Process ASGI Entrypoint (sync everything underneath)

**Part:** Foundation
**Depends on:** Phase 0
**Usable after:** yes — identical behavior, new server, `python3 app.py` boots it. Drop gevent from serve path.

Smallest possible cutover: one process serves, everything else unchanged. **Reuse Werkzeug — do not hand-roll Request/Response compat.** Werkzeug's Request is just an `environ` parser; it doesn't need a WSGI server.

## Tasks

- **`app.py` entrypoint**:
 builds the loop, starts `uvicorn.Server(...).serve()` as a task — later phases add queue/scheduler/socket.io tasks to the same loop via ASGI lifespan (`startup`/`shutdown`).
- **`frappe/asgi.py` ASGI callable** (no Starlette) — raw `scope`/`receive`/`send`.
- **Environ adapter**: build a WSGI `environ` from the ASGI scope (body buffered/streamed from `receive` into a readable stream) and feed the existing Werkzeug-based handler **unchanged**. No custom Request class, no `python-multipart` — Werkzeug already parses multipart, content negotiation, range, conditional requests from environ. A native ASGI Request is a later optimization, only if profiling demands it.
- **Response conversion**: Werkzeug `Response` → ASGI `http.response.start`/`body` sends (status, headers, body, cookies). Must handle **streamed/iterator bodies** (file downloads, `direct_passthrough`) by iterating and sending chunks — never materialize.
- Run the wrapped handler via `sync_to_async(handler, thread_sensitive=False)` on the sized pool.
- **`finally` on every path**: call `.close()` on the Werkzeug response iterator AND `frappe.destroy()` — known pitfall from this bench: asgiref never closed the WSGI iterator → leaked 1 DB conn/request → `1040 Too many connections`.

## Architecture context

Light mode = default: single process, single asyncio loop, started with `python3 app.py` — uvicorn run programmatically (`uvicorn.Server(config).serve()` awaited inside the loop), never the uvicorn CLI, never `--workers`.

```
python3 app.py
  └── one asyncio loop
        ├── uvicorn server (HTTP/ASGI)
        ├── task queue workers (3–4 asyncio tasks)
        ├── scheduler tick task
        └── socket.io server
```

Concurrency model: all `sync_to_async` calls use `thread_sensitive=False` with a sized `ThreadPoolExecutor` (~2×CPU workers) set as the loop's default executor. `thread_sensitive=True` is the 16-rps trap — never use it.

## Memory: the entrypoint owns process config

The single long-lived process is the headline memory win (~10–15 procs → 1), but it loses gunicorn's `max_requests` worker-recycling — which silently kept heap fragmentation from accumulating. A process that runs for weeks never gives memory back unless we make it. The entrypoint owns:

- **Memory hygiene (long-lived process)** — biggest *new* risk:
  - `MALLOC_ARENA_MAX=2` — glibc spawns one malloc arena per thread by default; the thread pool multiplies arenas and fragmentation. Cap it.
  - Preload **jemalloc or tcmalloc** (`LD_PRELOAD`) — markedly less fragmentation than glibc malloc for long-lived Python.
  - Periodic `gc.collect()` + `ctypes` `malloc_trim(0)` on an idle-tick — release freed pages back to the OS.
  - Optional **RSS soft-restart**: supervisor restarts the process past an RSS threshold — explicit replacement for gunicorn `max_requests`. Light mode loses running jobs on restart → depends on the Phase 9 stuck-job reaper to requeue them.
- **Thread pool is the new memory cost** — 2×CPU threads each reserve a stack (8 MB default) + hold the heap of whatever sync handler runs concurrently:
  - Pool size **configurable**, default conservatively (tie to expected *sync* concurrency, not blindly 2×CPU).
  - Set a smaller `threading.stack_size()` for pool threads.
  - As code ports to async (Phases 8, 15–17), **shrink the pool** — track pool size as a per-phase target alongside RSS.
- **Bounded in-flight requests**: set uvicorn `limit_concurrency` / backpressure caps so the count of live request objects (and their thread-pool fan-out) is bounded under load — caps peak RSS during spikes instead of letting it balloon.
