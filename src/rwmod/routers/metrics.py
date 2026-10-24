"""Prometheus metrics — exposes /metrics endpoint for Grafana dashboards.

Tracks: request counts by endpoint, latencies, error rates, queue depth,
mod count, and Steam API connectivity. All metrics use the Prometheus
text exposition format for direct Grafana consumption.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["metrics"])

# ── metrics state ──────────────────────────────────────────────────
_start_time = time.time()

# Per-endpoint: { "GET /api/mods": {"count": N, "total_ms": float, "errors": N} }
_request_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_ms": 0.0, "errors": 0})

# Cardinality bounds. Path parameters must be collapsed to their route
# template ("/api/tags/by-tag/{tag}") and the number of series capped, or an
# attacker can grow ``_request_stats`` without bound (one key per unique path)
# and exhaust memory.
MAX_METRIC_SERIES = 512
MAX_METRIC_KEY_CHARS = 256
_METRIC_OVERFLOW_KEY = "__other__"


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metric_path(request: Request) -> str:
    """Use the matched route template, not the raw (attacker-controlled) path."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return request.url.path


def _metric_key(method: str, path: str) -> str:
    """Bound the series key: collapse path params and cap cardinality/length."""
    # HTTP methods are client-controlled too (any token is legal on the wire).
    if len(method) > 16 or not method.isalpha() or not method.isupper():
        method = "OTHER"
    if len(path) > MAX_METRIC_KEY_CHARS:
        path = _METRIC_OVERFLOW_KEY
    key = f"{method} {path}"
    if key not in _request_stats and len(_request_stats) >= MAX_METRIC_SERIES:
        return f"{method} {_METRIC_OVERFLOW_KEY}"
    return key


def record_request(request: Request, status_code: int, elapsed_ms: float) -> None:
    """Record request metrics. Called from server middleware."""
    key = _metric_key(request.method, _metric_path(request))
    stats = _request_stats[key]
    stats["count"] += 1
    stats["total_ms"] += elapsed_ms
    if status_code >= 400:
        stats["errors"] += 1


_GAUGE_TYPES = {
    "active_downloads": int,
    "mod_count": int,
    "disk_usage_mb": float,
    "steam_online": bool,
    "queue_depth": int,
}

_GAUGE_STORE: dict[str, float | bool | int] = {
    "active_downloads": 0,
    "mod_count": 0,
    "disk_usage_mb": 0.0,
    "steam_online": True,
    "queue_depth": 0,
}


def set_gauge(key: str, value: float | bool | int) -> None:
    """Set gauge values from app modules."""
    if cast := _GAUGE_TYPES.get(key):
        _GAUGE_STORE[key] = cast(value)


@router.get("/metrics")
def metrics():
    """Prometheus-compatible metrics endpoint.

    Gated like every other route — it exposes request volume / download
    activity to anyone on the LAN otherwise.
    """
    uptime = time.time() - _start_time
    lines = [
        "# HELP rwmod_uptime_seconds Server uptime in seconds",
        "# TYPE rwmod_uptime_seconds gauge",
        f"rwmod_uptime_seconds {uptime:.1f}",
        "",
        "# HELP rwmod_requests_total Total requests by endpoint",
        "# TYPE rwmod_requests_total counter",
    ]
    for key, stats in sorted(_request_stats.items()):
        method, path = key.split(" ", 1)
        lines.append(
            f'rwmod_requests_total{{method="{_escape_label(method)}",'
            f'path="{_escape_label(path)}"}} {stats["count"]}'
        )

    lines.extend(
        [
            "",
            "# HELP rwmod_request_duration_ms_total Total request duration by endpoint",
            "# TYPE rwmod_request_duration_ms_total counter",
        ]
    )
    for key, stats in sorted(_request_stats.items()):
        method, path = key.split(" ", 1)
        lines.append(
            f'rwmod_request_duration_ms_total{{method="{_escape_label(method)}",'
            f'path="{_escape_label(path)}"}} {stats["total_ms"]:.0f}'
        )

    lines.extend(
        [
            "",
            "# HELP rwmod_errors_total Error responses (4xx/5xx) by endpoint",
            "# TYPE rwmod_errors_total counter",
        ]
    )
    for key, stats in sorted(_request_stats.items()):
        if stats["errors"] > 0:
            method, path = key.split(" ", 1)
            lines.append(
                f'rwmod_errors_total{{method="{_escape_label(method)}",'
                f'path="{_escape_label(path)}"}} {stats["errors"]}'
            )

    gauges = [
        (
            "rwmod_active_downloads",
            "Currently active downloads",
            "gauge",
            _GAUGE_STORE["active_downloads"],
        ),
        ("rwmod_mod_count", "Installed mod count", "gauge", _GAUGE_STORE["mod_count"]),
        (
            "rwmod_disk_usage_mb",
            "Mod directory disk usage in MB",
            "gauge",
            f"{_GAUGE_STORE['disk_usage_mb']:.1f}",
        ),
        (
            "rwmod_queue_depth",
            "Pending + downloading queue items",
            "gauge",
            _GAUGE_STORE["queue_depth"],
        ),
        (
            "rwmod_steam_api_up",
            "Steam API reachable (1=yes, 0=no)",
            "gauge",
            1 if _GAUGE_STORE["steam_online"] else 0,
        ),
    ]
    for name, help_text, mtype, value in gauges:
        lines.extend(
            ["", f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}", f"{name} {value}"]
        )

    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")
