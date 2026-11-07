"""rwmod Web Server — FastAPI app factory.

This is the top-level entry point. All routes are organized in
src/rwmod/routers/ by domain concern (mods, downloads, backups, etc.).
Dependencies (config, DB, queue) are injected via src/rwmod/deps.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from rwmod import __version__
from rwmod.database import close_db, init_db
from rwmod.deps import get_autoupdate
from rwmod.errors import RwmodError
from rwmod.logger import get_log, init_logging
from rwmod.models.schemas import ErrorResponse
from rwmod.queue import get_queue

# ── routers ────────────────────────────────────────────────────────
from rwmod.routers.auto_update import router as autoupdate_router
from rwmod.routers.backups import router as backups_router
from rwmod.routers.compat import router as compat_router
from rwmod.routers.config import router as config_router
from rwmod.routers.dashboard import router as dashboard_router
from rwmod.routers.download import router as download_router
from rwmod.routers.health import router as health_router
from rwmod.routers.history import router as history_router
from rwmod.routers.metrics import record_request, set_gauge
from rwmod.routers.metrics import router as metrics_router
from rwmod.routers.mod_settings import router as mod_settings_router
from rwmod.routers.mods import router as mods_router
from rwmod.routers.profiles import router as profiles_router
from rwmod.routers.queue import router as queue_router
from rwmod.routers.rimsort import router as rimsort_router
from rwmod.routers.saves import router as saves_router
from rwmod.routers.tags import router as tags_router
from rwmod.routers.transfer import router as transfer_router
from rwmod.routers.undo import router as undo_router
from rwmod.routers.workshop import router as workshop_router
from rwmod.utils import bundle_root

# Resolved via bundle_root(): works from a source checkout AND from a
# PyInstaller one-file EXE (where __file__ lives in a temp dir, not the bundle).
STATIC_DIR = bundle_root() / "static"

# Connected WebSocket clients (for queue status broadcasts to the UI).
_ws_clients: set[WebSocket] = set()
# Per-client outbound message backlog — used as backpressure: a client that
# stops reading (half-open TCP connection) keeps the backlog growing, and we
# drop it once the backlog exceeds the cap instead of buffering forever.
_ws_pending: dict[WebSocket, int] = {}
_WS_PENDING_MAX = 200

# Track fire-and-forget broadcast tasks so the GC never destroys them before
# they finish ("Task was destroyed but it is pending!").
_background_tasks: set[asyncio.Task] = set()

init_logging()
_log = get_log("rwmod.server")
_log.info("Server starting — log: %s", Path.home() / ".rwmod.log")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    au = get_autoupdate()
    await au.start_background()
    set_gauge("steam_online", True)

    # Push real-time queue snapshots to all connected WebSocket clients.
    get_queue().on_update(broadcast_queue_update)

    # Push a notification to the UI when background update checks find updates.
    au.on_update(broadcast_update_notification)

    yield
    await au.stop_background()
    # Stop the queue worker so it stops draining; the in-flight SteamCMD
    # thread may still finish in the background, but the event loop no longer
    # owns tasks that write to the DB after close_db() below.
    queue = get_queue()
    queue.stop()
    await queue.wait_stopped(timeout=3)
    close_db()


app = FastAPI(
    title="rwmod Web",
    # pyproject.toml is the single source of truth; run_tests.py asserts that
    # rwmod.__version__, this app version and rwmod.iss AppVersion all agree.
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


def _custom_openapi() -> dict:
    """Generate the schema, then make the documented 422 match reality.

    FastAPI documents every 422 as its default ``HTTPValidationError``
    (``{"detail": [...]}``), but validation_error_handler answers
    ``{error, detail: str}`` like every other failure. Left alone, /api/docs
    would describe a body the server no longer sends.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    error_schema = ErrorResponse.model_json_schema()
    error_schema.pop("$defs", None)  # no nested models to hoist
    schema.setdefault("components", {}).setdefault("schemas", {})["ErrorResponse"] = error_schema
    ref = {"$ref": "#/components/schemas/ErrorResponse"}

    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            documented = operation.get("responses", {}).get("422")
            if isinstance(documented, dict) and "content" in documented:
                documented["content"]["application/json"]["schema"] = ref

    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]  # documented FastAPI pattern

# ── request body limits ────────────────────────────────────────────
# The largest legitimate JSON body is a full ModsConfig.xml (a few hundred KB
# for a 500-mod list); every other JSON payload is tiny. Without a cap a LAN
# client can make the process buffer an arbitrarily large body and OOM it.
# Multipart uploads keep their own, much larger per-endpoint limits.
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024

# Browser WebSocket handshakes always carry Origin; a cross-site page must not
# be able to subscribe to the queue feed. Localhost in any port is allowed
# (matching the CORS policy) plus any same-origin request (LAN access is a
# supported deployment).
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.I)


def _origin_allowed(origin: str, host: str) -> bool:
    """True for a same-origin or loopback Origin header."""
    if _LOCAL_ORIGIN_RE.match(origin):
        return True
    if host:
        return origin.rstrip("/").split("://", 1)[-1].lower() == host.lower()
    return False


class JsonBodyLimitMiddleware:
    """Cap ``application/json`` request bodies, chunked uploads included.

    ``Content-Length`` is absent for a chunked body, so the cap cannot rely on
    it; the body is drained here (at most ``max_bytes + 1`` bytes are kept) and
    replayed to the application. Only JSON is affected — file uploads stream
    through untouched and keep their endpoint-specific limits.
    """

    def __init__(self, app: Any, max_bytes: int = MAX_JSON_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_type = ""
        declared: int | None = None
        for name, value in scope.get("headers", []):
            if name == b"content-type":
                content_type = value.decode("latin-1").lower()
            elif name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
        if "application/json" not in content_type:
            await self.app(scope, receive, send)
            return
        if declared is not None and declared > self.max_bytes:
            await self._too_large(send)
            return

        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await self.app(scope, receive, send)
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._too_large(send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return cast("dict[str, Any]", await receive())

        await self.app(scope, replay, send)

    async def _too_large(self, send: Any) -> None:
        payload = json.dumps(
            {
                "error": "PayloadTooLarge",
                "detail": f"请求体过大（上限 {self.max_bytes // (1024 * 1024)} MB）",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


# ── middleware ─────────────────────────────────────────────────────
app.add_middleware(JsonBodyLimitMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Apply baseline hardening headers to every HTTP response.

    The CSP is attached to HTML documents served outside /api only. The React UI
    (GET /) is the one document that runs scripts, and its bundle ships no inline
    <script> or inline event handlers, so script-src 'self' is safe.
    style-src keeps 'unsafe-inline' for React style props and third-party CSS
    variables. /api/docs (Swagger UI) is excluded because it loads its JS from a
    CDN; static assets inherit the page's policy and need no header of their own.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html") and not request.url.path.startswith("/api"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
    # Static assets may be cached; API responses get explicit no-store so a
    # logged-out stale page never leaks through a browser back/forward cache.
    if request.url.path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.middleware("http")
async def request_tracing(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Add X-Request-ID + log timing + record metrics."""
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    response.headers["X-Request-ID"] = req_id

    flag = " ⚠ SLOW" if elapsed_ms > 500 else ""
    _log.info(
        "[%s] %s %s → %s (%sms)%s",
        req_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        flag,
    )

    record_request(request, response.status_code, elapsed_ms)
    return response


# ── global error handler ───────────────────────────────────────────


@app.exception_handler(RwmodError)
async def rwmod_error_handler(request: Request, exc: RwmodError) -> JSONResponse:
    """Map all RwmodError subclasses to structured JSON responses."""
    _log.warning("%s %s → %s: %s", request.method, request.url.path, exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": type(exc).__name__, "detail": exc.detail},
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Normalise plain HTTPException into the one failure shape.

    Routers still raise HTTPException in ~45 places; FastAPI's default body for
    it is a bare {"detail": str}, which would leave the frontend parsing two
    shapes forever. Normalising here makes the client's job complete today, and
    lets those sites migrate to errors.py one at a time without any client-side
    change. `error` stays generic ("HTTPError") so a name like ModNotFoundError
    only ever means the errors.py class was actually raised — the generic value
    is a visible marker of a site still to migrate.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "HTTPError", "detail": str(exc.detail)},
        headers=getattr(exc, "headers", None),
    )


def _describe_validation_errors(exc: RequestValidationError) -> str:
    """Flatten FastAPI's error list into one readable detail string."""
    parts: list[str] = []
    for err in exc.errors():
        # "body" is noise — the client already knows which request it sent.
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = str(err.get("msg", ""))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(p for p in parts if p) or "请求体无效"


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Give body/query validation failures the same shape as every other error.

    FastAPI's default is a bare {"detail": [ {...}, ... ]} — a fifth error shape
    for the frontend to parse, and a *list* where every other detail is a string.
    The status stays 422 because clients already branch on it; only the body is
    brought in line with the failure contract.
    """
    detail = _describe_validation_errors(exc)
    _log.warning("%s %s → 422 校验失败: %s", request.method, request.url.path, detail)
    return JSONResponse(status_code=422, content={"error": "ValidationError", "detail": detail})


@app.exception_handler(Exception)
async def catchall_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unhandled exceptions — log full traceback, return 500."""
    _log.error("未处理异常: %s %s — %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "InternalError", "detail": "内部服务器错误"},
    )


# ── routers ────────────────────────────────────────────────────────
app.include_router(health_router)
app.include_router(config_router)
app.include_router(dashboard_router)
app.include_router(mods_router)
app.include_router(mod_settings_router)
app.include_router(download_router)
app.include_router(workshop_router)
app.include_router(queue_router)
app.include_router(backups_router)
app.include_router(compat_router)
app.include_router(profiles_router)

app.include_router(rimsort_router)
app.include_router(history_router)
app.include_router(autoupdate_router)
app.include_router(metrics_router)
app.include_router(tags_router)
app.include_router(saves_router)
app.include_router(transfer_router)
app.include_router(undo_router)


# ── static files ───────────────────────────────────────────────────
@app.get("/")
def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        # static/ is a build artifact and is not in git, so a fresh clone lands here.
        # FileResponse would fail with an opaque 500 — say what to do instead.
        raise HTTPException(
            status_code=503,
            detail=(
                "前端未构建：static/index.html 不存在。请先构建前端："
                "cd frontend && npm install && npm run build"
                "（或 python tools/build_frontend.py），然后重启服务。"
            ),
        )
    return FileResponse(index_path)


# ── WebSocket ─────────────────────────────────────────────────────


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """WebSocket for real-time status updates to frontend.

    Browsers always send ``Origin`` on a WebSocket handshake, so a cross-site
    page (or a DNS-rebinding host) is rejected here before it ever sees queue
    data. Non-browser clients (CLI, tests, native shells) send no Origin and
    are allowed — they are not a CSRF vector.
    """
    origin = ws.headers.get("origin")
    if origin and not _origin_allowed(origin, ws.headers.get("host", "")):
        _log.warning("拒绝跨站 WebSocket 握手: origin=%s", origin)
        await ws.close(code=1008)
        return
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "msg": "invalid json"})
                continue

            cmd = msg.get("cmd", "ping")
            if cmd == "ping":
                pending = _queue_pending_count()
                set_gauge("queue_depth", pending)
                await ws.send_json({"type": "pong", "queue_pending": pending})
            elif cmd == "subscribe":
                await ws.send_json({"type": "subscribed", "topic": msg.get("topic", "all")})
            else:
                await ws.send_json({"type": "echo", "cmd": cmd})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        _ws_pending.pop(ws, None)


async def _broadcast_ws(payload: dict) -> None:
    """Send a JSON message to every connected WebSocket client.

    Sends to all clients concurrently so a slow/stalled socket can never
    block the broadcast to the others (a sequential await would let one
    slow client stall the whole event loop's queue notifications). Clients
    whose outbound backlog exceeds _WS_PENDING_MAX (stuck/half-open sockets)
    are dropped instead of buffering unboundedly.
    """
    clients = list(_ws_clients)
    if not clients:
        return

    async def _send(ws: WebSocket) -> None:
        if _ws_pending.get(ws, 0) >= _WS_PENDING_MAX:
            _log.warning("WS 客户端积压过多，断开: %s", ws.client)
            _ws_clients.discard(ws)
            _ws_pending.pop(ws, None)
            with suppress(Exception):
                await ws.close(code=1008)
            return
        _ws_pending[ws] = _ws_pending.get(ws, 0) + 1
        try:
            await ws.send_json(payload)
        except Exception:  # noqa: BLE001 — a dead socket must not break the loop
            _ws_clients.discard(ws)
            _ws_pending.pop(ws, None)
        else:
            remaining = _ws_pending.get(ws, 1) - 1
            if remaining <= 0:
                _ws_pending.pop(ws, None)
            else:
                _ws_pending[ws] = remaining

    await asyncio.gather(*(_send(ws) for ws in clients))


async def broadcast_queue_update(snapshot: list[dict]) -> None:
    """Registered as the queue's on_update callback — pushes snapshots to UI."""
    await _broadcast_ws({"type": "queue_update", "items": snapshot})


def broadcast_update_notification(updates: list[dict]) -> None:
    """Sync callback from AutoUpdateManager — schedules a WS notification.

    AutoUpdateManager._notify runs in the event loop thread, so we can safely
    schedule the async broadcast without awaiting it here.
    """
    task = asyncio.create_task(
        _broadcast_ws({"type": "update_notification", "count": len(updates), "updates": updates})
    )
    # Keep a reference so the task is never GC'd mid-flight.
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _queue_pending_count() -> int:
    try:
        items = get_queue().snapshot()
        return sum(1 for i in items if i["status"] in ("pending", "downloading"))
    except Exception:
        return 0


if STATIC_DIR.exists():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("rwmod.server:app", host="0.0.0.0", port=8000, reload=False)  # nosec B104 — LAN access by design
