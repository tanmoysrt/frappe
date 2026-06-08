# Frappe Async Migration Plan

> ⚠️ **SUPERSEDED by [plan.md](plan.md) + [specs/](specs/).** This is the early design dump. Code blocks below are historical and contain known defects (flagged inline) — **do not implement from this file.** Kept for the socket.io handler inventory and narrative. Authoritative design lives in `plan.md` and the per-phase `specs/`.

## Current State

- WSGI (Werkzeug) + gevent ASGI adapter
- Sync DB drivers (pymysql/psycopg2), sync redis-py
- RQ for background jobs (fork-based, Redis)
- `frappe.local` uses `ContextVar` (async-safe)

---

## Design: Async First + Uvicorn

- **Uvicorn** is the single server (no WSGI/gevent)
- **Async is primary** — one code path to maintain
- **Sync runs via `asgiref.sync_to_async`** — thread pool, no blocking
- `frappe.enqueue()` API stays identical

### Request Dispatch

> ❌ **DEFECT — do not implement.** `thread_sensitive=True` is the 16-rps trap (see plan.md §1.3): it serializes ALL sync handlers onto one shared thread, concurrency=1. Use `thread_sensitive=False` + a sized `ThreadPoolExecutor`. `frappe.local` is ContextVar-backed, so this is safe — asgiref copies the context into the worker thread and propagates writes back. The claim below is wrong.

```
Request → Uvicorn
        → is_async(handler)? → await handler()
        → is_sync(handler)?  → await sync_to_async(handler, thread_sensitive=True)()
```

`thread_sensitive=True` ensures `frappe.local` context stays consistent.

<details>
<summary>Async Detection + Router Dispatch</summary>

```python
import asyncio
import inspect
from asgiref.sync import sync_to_async

def is_async_callable(func):
    """Check if a function is async, even through decorators."""
    unwrapped = inspect.unwrap(func, stop=lambda f: hasattr(f, '__wrapped__') is False)
    return asyncio.iscoroutinefunction(unwrapped)

async def dispatch(handler, *args, **kwargs):
    """Route to async or sync handler automatically."""
    if is_async_callable(handler):
        return await handler(*args, **kwargs)
    else:
        return await sync_to_async(handler, thread_sensitive=True)(*args, **kwargs)

# Frappe RPC integration
async def handle_rpc(method_path, *args, **kwargs):
    method = get_whitelisted_method(method_path)
    return await dispatch(method, *args, **kwargs)
```

### Edge Cases Handled

| Scenario | Detection |
|----------|-----------|
| `async def foo()` | `iscoroutinefunction` → True |
| `def foo()` | `iscoroutinefunction` → False |
| `@wraps` decorated async | `unwrap` follows `__wrapped__` → True |
| Class with `__call__` async | Check `iscoroutinefunction(obj.__call__)` |
| `functools.partial` | `unwrap` handles partial |
| `@frappe.whitelist()` wrapped | `unwrap` follows chain → detects original |

### How `inspect.unwrap` Works

```python
def my_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper

@my_decorator
async def original():
    pass

inspect.iscoroutinefunction(original)       # False — sees wrapper
inspect.iscoroutinefunction(inspect.unwrap(original))  # True — finds async def
```

</details>

---

## Migration Phases

| Phase | What | Risk |
|-------|------|------|
| 1 | Uvicorn + async Redis + aiofiles | Low |
| 2 | Async DB drivers (asyncpg/aiomysql) + connection pooling | Medium |
| 3 | Async `app.py`, async whitelisted methods | Medium |
| 4 | Replace RQ with task queue, async scheduler, aiosmtplib | Higher |
| 5 | Async Document class (`await doc.save()`) | Highest |

**Timeline: 9-17 months**

---

## Async-First Pattern

Primary is async, sync is a thin wrapper. Zero code duplication.

<details>
<summary>Database Layer Example</summary>

> ❌ **DEFECT — do not implement.** Two bugs: (1) `get_value` is defined twice on the class below — the second (sync) shadows the first (async primary), making the async path unreachable. (2) `asyncio.run(coro)` spins up AND tears down a fresh event loop on every sync call from no-loop contexts (CLI/bench/patches) — catastrophic throughput, and breaks any loop-bound pool/connection. Correct approach (plan.md §2.3): one async implementation + a thin `asgiref.sync.async_to_sync` facade, which caches a loop in a thread. The `sync_wrapper` decorator further down has the same `asyncio.run`-per-call flaw.

```python
import asyncio

class Database:
    def __init__(self, async_conn):
        self._async_conn = async_conn  # asyncpg / aiomysql

    # Primary: async implementation
    async def get_value(self, doctype, name, field):
        query = f"SELECT {field} FROM `tab{doctype}` WHERE name = %s"
        result = await self._async_conn.execute(query, (name,))
        return result.fetchone()[0]

    # Compat: sync wrapper (calls async internally)
    def get_value(self, doctype, name, field):
        coro = self._get_value_async(doctype, name, field)
        try:
            loop = asyncio.get_running_loop()
            return coro  # In async context — caller must await
        except RuntimeError:
            return asyncio.run(coro)  # No loop — run synchronously

    async def _get_value_async(self, doctype, name, field):
        query = f"SELECT {field} FROM `tab{doctype}` WHERE name = %s"
        result = await self._async_conn.execute(query, (name,))
        return result.fetchone()[0]
```

### Usage

```python
# Async (preferred)
@frappe.whitelist()
async def async_handler():
    email = await frappe.db.get_value("User", "test@example.com", "email")
    user, profile = await asyncio.gather(
        frappe.db.get_value("User", "test@example.com", "email"),
        frappe.db.get_value("Profile", "test@example.com", "bio"),
    )

# Sync (backward compat, calls async internally)
def sync_handler():
    email = frappe.db.get_value("User", "test@example.com", "email")
```

### Same Pattern for Cache, ORM, Email

```python
# Cache
cache_val = await frappe.cache().get_value("my_key")  # async
cache_val = frappe.cache().get_value("my_key")        # sync compat

# ORM
doc = await frappe.get_doc("User", "test@example.com")  # async
await doc.save()
doc = frappe.get_doc("User", "test@example.com")        # sync compat
doc.save()

# Email
await frappe.sendmail(recipients=["user@example.com"], message="Hello")  # async
frappe.sendmail(recipients=["user@example.com"], message="Hello")        # sync compat
```

### Reusable Sync Wrapper Decorator

```python
from functools import wraps

def sync_wrapper(async_func):
    """Wrap an async function to work in sync context."""
    @wraps(async_func)
    def wrapper(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            return async_func(*args, **kwargs)  # Return coroutine
        except RuntimeError:
            return asyncio.run(async_func(*args, **kwargs))  # Run sync
    return wrapper

class Database:
    @sync_wrapper
    async def get_value(self, doctype, name, field):
        ...
```

</details>

---

## Background Jobs: Two Options

RQ is incompatible with uvicorn (fork-based workers break event loop). Two replacements:

### Option A: SQLite Task Queue (Default — Lightweight)

- Zero external deps, runs in uvicorn's event loop
- WAL mode + 3-4 workers, `busy_timeout=5000`
- Good for moderate load (~100 jobs/sec)
- Single process, no inter-process overhead

<details>
<summary>SQLite Task Queue Implementation</summary>

### Schema

```sql
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    queue TEXT NOT NULL,           -- short, default, long
    func TEXT NOT NULL,            -- "module.submodule.function_name"
    kwargs TEXT,                   -- JSON serialized kwargs
    status TEXT DEFAULT 'pending', -- pending, running, completed, failed
    retries INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    error TEXT,                    -- traceback on failure
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_queue ON jobs(queue, status);
```

### Implementation

```python
import asyncio
import aiosqlite
import json
import importlib
from datetime import datetime

class TaskQueue:
    def __init__(self, db_path="frappe_jobs.db"):
        self._db = None
        self._db_path = db_path
        self._signal = asyncio.Event()
        self._workers = []
        self._running = False

    async def init(self):
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, queue TEXT NOT NULL,
                func TEXT NOT NULL, kwargs TEXT,
                status TEXT DEFAULT 'pending', retries INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3, error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP, completed_at TIMESTAMP
            )
        """)
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(queue, status)")
        await self._db.commit()

    async def enqueue(self, func, queue="default", **kwargs):
        func_name = func if isinstance(func, str) else func.__module__ + "." + func.__qualname__
        await self._db.execute(
            "INSERT INTO jobs (queue, func, kwargs) VALUES (?, ?, ?)",
            (queue, func_name, json.dumps(kwargs))
        )
        await self._db.commit()
        self._signal.set()  # wake up sleeping workers

    async def start(self, concurrency=3):
        self._running = True
        for _ in range(concurrency):
            self._workers.append(asyncio.create_task(self._worker()))

    async def stop(self):
        self._running = False
        self._signal.set()
        await asyncio.gather(*self._workers, return_exceptions=True)
        await self._db.close()

    async def _worker(self):
        while self._running:
            job = await self._fetch_job()
            if job:
                await self._execute_job(job)
            else:
                self._signal.clear()
                await self._signal.wait()

    # ❌ DEFECT — do not implement. Racy claim: SELECT then a separate UPDATE with an
    # `await` between them lets two in-loop workers grab the same job. Use a single atomic
    # statement (plan.md Phase 9): UPDATE jobs SET status='running' WHERE id =
    # (SELECT id FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1) RETURNING ...
    # (requires SQLite >= 3.35).
    async def _fetch_job(self):
        async with self._db.execute(
            "SELECT id, func, kwargs FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            job_id, func_name, kwargs_json = row
            await self._db.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                (datetime.now().isoformat(), job_id)
            )
            await self._db.commit()
            return {"id": job_id, "func": func_name, "kwargs": json.loads(kwargs_json or "{}")}
        return None

    async def _execute_job(self, job):
        module_path, func_name = job["func"].rsplit(".", 1)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        try:
            if asyncio.iscoroutinefunction(func):
                await func(**job["kwargs"])
            else:
                from asgiref.sync import sync_to_async
                await sync_to_async(func)(**job["kwargs"])
            await self._db.execute(
                "UPDATE jobs SET status = 'completed', completed_at = ? WHERE id = ?",
                (datetime.now().isoformat(), job["id"])
            )
        except Exception as e:
            await self._handle_failure(job, e)
        await self._db.commit()

    async def _handle_failure(self, job, error):
        retries = job.get("retries", 0) + 1
        if retries < job.get("max_retries", 3):
            await self._db.execute(
                "UPDATE jobs SET status = 'pending', retries = ?, error = ? WHERE id = ?",
                (retries, str(error), job["id"])
            )
        else:
            await self._db.execute(
                "UPDATE jobs SET status = 'failed', retries = ?, error = ? WHERE id = ?",
                (retries, str(error), job["id"])
            )
```

### Frappe Integration

```python
# frappe/utils/background_jobs.py
_task_queue = None

def get_task_queue():
    global _task_queue
    if _task_queue is None:
        _task_queue = TaskQueue()
    return _task_queue

def enqueue(func, queue="default", **kwargs):
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(get_task_queue().enqueue(func, queue=queue, **kwargs))
    else:
        asyncio.run(get_task_queue().enqueue(func, queue=queue, **kwargs))
```

</details>

### Option B: ARQ (Advanced — When You Need More)

- Built on `asyncio` + `redis.asyncio`
- Distributed workers, pub/sub, high throughput (10k+/sec)
- Use when: multi-server, cron scheduling, real-time progress, arq-dashboard monitoring

<details>
<summary>ARQ Example</summary>

```python
# worker.py
from arq import create_arq_worker
from arq.connections import RedisSettings

async def send_email(ctx, recipients, subject, body):
    await frappe.sendmail(recipients=recipients, subject=subject, message=body)

async def process_import(ctx, file_path, doctype):
    await frappe.import_file(doctype, file_path)

class WorkerSettings:
    functions = [send_email, process_import]
    redis_settings = RedisSettings(host="localhost", port=6379)
    max_jobs = 10
    job_timeout = 300
```

```python
# Enqueue via ARQ
from arq.connections import ArqRedis

async def enqueue_job(func, queue="default", **kwargs):
    redis: ArqRedis = ctx["redis"]
    await redis.enqueue_job(func.__name__, **kwargs, _queue_name=queue)
```

</details>

### Comparison

| Feature | SQLite | ARQ (Redis) |
|---------|--------|-------------|
| External deps | None | Redis |
| Workers | 3-4 in-process | Separate process, scalable |
| Distributed | No | Yes |
| Throughput | ~100/s | 10k+/s |
| Persistence | SQLite file | Redis (needs AOF/RDB) |
| Pub/sub | No | Yes |
| Monitoring | Query the DB | arq-dashboard |
| Best for | Single server | Multi-server |

**Recommendation**: SQLite default, switch to ARQ when scaling. `frappe.enqueue()` API stays identical.

---

## Key Challenges

| Challenge | Mitigation |
|-----------|------------|
| MariaDB async driver maturity | Start with PostgreSQL, thread pool for MariaDB |
| ORM complexity (2700+ lines) | Phase gradually, sync wrapper during transition |
| Third-party app compat | `sync_to_async` wraps sync handlers |
| Testing | Parallel test suite migration |

---


## Socket.IO: Node.js → Python Port

### Current Architecture (Node.js)

```
Browser → Node.js Socket.IO (separate process)
  → Auth: HTTP request to Frappe API
  → Redis pub/sub: Python publishes → Node consumes → emits to clients
```

### Simplified: Same Process, Same Event Loop

Socket.IO and HTTP server run as **separate tasks in the same asyncio loop**. No HTTP calls, no Redis pub/sub. Direct function calls and direct emit.

```
uvicorn event loop
  ├── Task 1: HTTP server (Frappe ASGI app)
  └── Task 2: Socket.IO server (python-socketio)
       └── Direct: import frappe.realtime.get_user_info → call()
       └── Direct: sio.emit() — no Redis needed
```

<details>
<summary>Simplified Implementation</summary>

```python
# frappe/realtime_server.py
import socketio
import asyncio
import frappe
from frappe.utils.data import cstr

sio = socketio.AsyncServer(
    cors_allowed_origins="*",
    cors_credentials=True,
    async_mode="asgi",
    cleanup_empty_child_namespaces=True,
)

# --- Auth: direct import, no HTTP request ---
@sio.on("connect")
async def on_connect(sid, environ):
    namespace = environ.get("PATH_INFO", "/").strip("/")
    
    # Validate site
    if namespace not in frappe.get_all_sites():
        raise ConnectionRefusedError("Invalid namespace")
    
    # Validate origin
    origin = environ.get("HTTP_ORIGIN", "")
    host = environ.get("HTTP_HOST", "")
    if _get_hostname(origin) != _get_hostname(host):
        raise ConnectionRefusedError("Invalid origin")
    
    # Parse cookie/auth header
    cookies = _parse_cookies(environ.get("HTTP_COOKIE", ""))
    auth_header = environ.get("HTTP_AUTHORIZATION")
    if not cookies.get("sid") and not auth_header:
        raise ConnectionRefusedError("No auth")
    
    # Direct function call — no HTTP round trip
    with frappe.init_site(namespace):
        if auth_header:
            frappe.set_header("Authorization", auth_header)
        else:
            frappe.set_cookie("sid", cookies["sid"])
        
        user_info = frappe.realtime.get_user_info()
        if not user_info:
            raise ConnectionRefusedError("Unauthorized")
    
    await sio.save_session(sid, {
        "user": user_info["user"],
        "user_type": user_info["user_type"],
        "installed_apps": user_info.get("installed_apps", []),
        "site": namespace,
    })

# --- Handlers: same logic, direct calls ---
@sio.on("ping")
async def on_ping(sid):
    await sio.emit("pong", room=sid)

@sio.on("doctype_subscribe")
async def on_doctype_subscribe(sid, doctype):
    if await _has_permission(sid, doctype):
        await sio.enter_room(sid, f"doctype:{doctype}")

@sio.on("doc_subscribe")
async def on_doc_subscribe(sid, doctype, docname):
    if await _has_permission(sid, doctype, docname):
        await sio.enter_room(sid, f"doc:{doctype}/{docname}")

@sio.on("doc_open")
async def on_doc_open(sid, doctype, docname):
    if await _has_permission(sid, doctype, docname):
        await sio.enter_room(sid, f"open_doc:{doctype}/{docname}")
        await _notify_doc_viewers(sid, doctype, docname)

@sio.on("doc_close")
async def on_doc_close(sid, doctype, docname):
    await sio.leave_room(sid, f"open_doc:{doctype}/{docname}")
    await _notify_doc_viewers(sid, doctype, docname)

@sio.on("task_subscribe")
async def on_task_subscribe(sid, task_id):
    await sio.enter_room(sid, f"task_progress:{task_id}")

@sio.on("disconnect")
async def on_disconnect(sid):
    session = await sio.get_session(sid)
    for dt, dn in session.get("subscribed_docs", []):
        await _notify_doc_viewers(sid, dt, dn)

async def _has_permission(sid, doctype, docname=None):
    """Direct import call — no HTTP."""
    return frappe.realtime.has_permission(doctype, docname or "")

async def _notify_doc_viewers(sid, doctype, docname):
    room = f"open_doc:{doctype}/{docname}"
    users = []
    for client_sid in sio.manager.get_participants(namespace="/", room=room):
        sess = await sio.get_session(client_sid)
        users.append(sess.get("user"))
    
    my_session = await sio.get_session(sid)
    if len(users) == 1 and users[0] == my_session.get("user"):
        return
    
    await sio.emit("doc_viewers", {
        "doctype": doctype, "docname": docname,
        "users": list(set(users)),
    }, room=room)
```

### Direct Emit from Python (No Redis)

```python
# frappe/realtime.py — simplified
from frappe.realtime_server import sio

def publish_realtime(event=None, message=None, room=None, user=None,
                     doctype=None, docname=None, task_id=None, after_commit=False):
    if not room:
        if task_id: room = f"task_progress:{task_id}"
        elif user: room = f"user:{user}"
        elif doctype and docname: room = f"doc:{doctype}/{docname}"
        else: room = "all"
    
    if after_commit:
        frappe.local._realtime_log.append([event, message, room])
        frappe.db.after_commit.add(_flush_realtime_log)
    else:
        _emit(event, message, room)

def _emit(event, message, room):
    """Direct emit — same process, no Redis."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(sio.emit(event, message, room=room))
    except RuntimeError:
        sio.emit(event, message, room=room)
```


### Frappe ASGI App (No Starlette)

Frappe's own `app.py` becomes the ASGI callable. No Starlette needed.

<details>
<summary>Custom Request Class (Backward Compat)</summary>

Uvicorn passes raw ASGI `scope`, not a Request object. We build a compat Request class that mimics Werkzeug's API so `frappe.request.headers`, `frappe.request.cookies`, etc. keep working.

> ❌ **DEFECT — do not implement.** Hand-rolling this class is the wrong call (plan.md Phase 1): (1) `args` returns `parse_qs` lists, not Werkzeug's `MultiDict` — silently breaks every `request.args["x"]` call-site; (2) hand-rolled multipart vs Werkzeug's battle-tested parser; (3) the ASGI app further down materializes the whole response body — no streamed/iterator handling, so file downloads OOM. Correct approach: reuse Werkzeug as an `environ` parser (it doesn't need a WSGI server), build `environ` from the ASGI scope, feed the existing handler unchanged, and stream iterator response bodies.

```python
# frappe/request.py
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

class Request:
    """ASGI-compatible Request class, mimicking Werkzeug's Request API."""
    
    def __init__(self, scope, receive):
        self.scope = scope
        self._receive = receive
        self._body = None
        self._form = None
    
    @property
    def method(self):
        return self.scope["method"]
    
    @property
    def path(self):
        return self.scope["path"]
    
    @property
    def query_string(self):
        return self.scope.get("query_string", b"").decode()
    
    @property
    def args(self):
        """Query params — same as Werkzeug's request.args."""
        return parse_qs(self.query_string)
    
    @property
    def headers(self):
        """Dict of headers — same as Werkzeug's request.headers."""
        if not hasattr(self, "_headers"):
            self._headers = {
                k.decode().lower(): v.decode()
                for k, v in self.scope["headers"]
            }
        return self._headers
    
    @property
    def cookies(self):
        """Dict of cookies — same as Werkzeug's request.cookies."""
        cookie_header = self.headers.get("cookie", "")
        if not cookie_header:
            return {}
        return {k: v.value for k, v in SimpleCookie(cookie_header).items()}
    
    @property
    def content_type(self):
        return self.headers.get("content-type", "")
    
    @property
    def content_length(self):
        return int(self.headers.get("content-length", 0))
    
    async def data(self):
        """Raw body bytes — same as Werkzeug's request.data."""
        if self._body is None:
            body = b""
            while True:
                message = await self._receive()
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            self._body = body
        return self._body
    
    async def get_data(self, as_text=False):
        data = await self.data()
        return data.decode("utf-8") if as_text else data
    
    async def form(self):
        """POST form data — same as Werkzeug's request.form."""
        if self._form is None:
            raw = await self.data()
            content_type = self.content_type
            if "application/x-www-form-urlencoded" in content_type:
                self._form = parse_qs(raw.decode("utf-8"))
            elif "multipart/form-data" in content_type:
                self._form = await self._parse_multipart(raw)
            else:
                self._form = {}
        return self._form
    
    @property
    def url(self):
        scheme = self.scope.get("scheme", "http")
        host = self.headers.get("host", "localhost")
        return f"{scheme}://{host}{self.path}"
    
    @property
    def remote_addr(self):
        client = self.scope.get("client")
        return client[0] if client else None
```

### Frappe ASGI App

```python
# frappe/app.py — ASGI-native
from frappe.request import Request

async def application(scope, receive, send):
    if scope["type"] not in ("http", "websocket"):
        return
    
    if scope["type"] == "websocket":
        await handle_websocket(scope, receive, send)
        return
    
    # Build compat Request object
    request = Request(scope, receive)
    
    # Initialize site context
    site = get_site_from_request(request)
    frappe.init(site=site)
    frappe.connect()
    
    # Store request — backward compat with frappe.request.headers etc.
    frappe.local.request = request
    
    # Route to handler
    if request.path.startswith("/api/method/"):
        response = await handle_rpc(request)
    elif request.path.startswith("/api/"):
        response = await handle_rest(request)
    else:
        response = await handle_website(request)
    
    # Send ASGI response
    await send({
        "type": "http.response.start",
        "status": response.status_code,
        "headers": list(response.headers.items()),
    })
    await send({
        "type": "http.response.body",
        "body": response.body,
    })
    
    # Cleanup
    frappe.destroy()
```

### Backward Compat: What Stays the Same

```python
# All these keep working unchanged:
frappe.request.headers["content-type"]
frappe.request.cookies["sid"]
frappe.request.args["name"]
frappe.request.form["doctype"]
frappe.request.data
frappe.request.method
frappe.request.path
frappe.request.url
frappe.request.remote_addr
```

</details>

### Run as Separate Tasks in Same Loop

```python
# frappe/asgi.py
from frappe.app import application as http_app
from frappe.realtime_server import sio
import socketio

# Mount Socket.IO alongside Frappe HTTP app
# Both share the same event loop — no separate process
async def combined_app(scope, receive, send):
    if scope["path"].startswith("/socket.io"):
        socketio_app = socketio.ASGIApp(sio)
        await socketio_app(scope, receive, send)
    else:
        await http_app(scope, receive, send)

# uvicorn frappe.asgi:combined_app
```

</details>
### Comparison

| Aspect | Node.js (current) | Python (simplified) |
|--------|-------------------|---------------------|
| Process | Separate Node.js | Same uvicorn process |
| Auth | HTTP request to Frappe | Direct import + call |
| Pub/Sub | Redis required | Not needed — direct emit |
| Context | No `frappe.local` | Full Frappe context |
| Deploy | 2 processes | 1 process, 2 tasks |

### Migration Path

1. Add Python Socket.IO alongside Node.js
2. Switch clients to Python server
3. Remove Node.js `socketio.js` and `realtime/`

</details>
