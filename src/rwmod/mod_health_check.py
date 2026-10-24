"""Persistent, non-blocking Steam Workshop health cache for installed mods.

``/api/mods/health`` used to perform six Steam Web API batches synchronously
(100 ids each, 4 workers, 15s timeout) on every cold request — 10-16s typical
and up to 45s in the log — while ``/api/dashboard`` blocked on the same call.
This module stores the remote ``time_updated`` in the existing ``mod_cache``
table (6h TTL) and refreshes it in a daemon thread behind a single-flight
guard. Readers never wait for the network.

Failure policy: a batch whose response is empty for non-empty input is treated
as a failed batch, and its ids keep their previous cache rows. This matters
because the old code turned any missing response into ``removed`` — a transient
Steam outage would label every mod "下架". Only ids absent from a *successful*
response (and previously covered by it) are persisted as removed (sentinel -1).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rwmod.database import get_conn
from rwmod.metadata import ModMeta
from rwmod.mod_cache import get_cached_mods
from rwmod.workshop import _fetch_batch

__all__ = [
    "get_health",
    "refresh_async",
    "refresh_now",
    "stop_all",
    "wait_for_refresh",
]

_log = logging.getLogger(__name__)

_TTL_SECONDS = 6 * 60 * 60  # 6 hours — Steam update timestamps move slowly
_BATCH_SIZE = 100  # Steam caps GetPublishedFileDetails at 100 ids per call
_MAX_WORKERS = 4
_REMOVED_SENTINEL = -1  # mod_cache.time_updated marker: Steam did not return the id
_JOIN_TIMEOUT = 10.0

_lock = threading.Lock()
_refreshing: dict[str, threading.Thread] = {}


def _key(mods_dir: Path) -> str:
    return os.path.normcase(os.path.abspath(str(mods_dir)))


# ── read path ─────────────────────────────────────────────────────


def get_health(mods_dir: Path, *, start_refresh: bool = True) -> dict:
    """Return ``{"mods": [...], "stale": bool, "refreshing": bool}`` from cache.

    ``start_refresh=False`` is used by the dashboard: it must never trigger
    network work. A cold/expired cache reports status ``unknown`` (never
    ``removed``) and schedules a background refresh when allowed.
    """
    mods_path = Path(mods_dir)
    metas = [m for m in get_cached_mods(mods_path) if m.workshop_id]
    if not metas:
        return {"mods": [], "stale": False, "refreshing": False}

    rows = _read_rows([m.workshop_id for m in metas])
    now = time.time()
    results: list[dict] = []
    stale = False

    for meta in metas:
        row = rows.get(meta.workshop_id)
        if row is None:
            stale = True
            status, last_updated = "unknown", ""
        else:
            time_updated, age_seconds = row
            if age_seconds is None or age_seconds > _TTL_SECONDS:
                stale = True
            if time_updated == _REMOVED_SENTINEL:
                status, last_updated = "removed", ""
            else:
                age_days = max(0.0, (now - time_updated) / 86400.0)
                status = (
                    "maintained" if age_days < 90 else "stale" if age_days < 365 else "abandoned"
                )
                last_updated = time.strftime("%Y-%m-%d", time.gmtime(time_updated))
        results.append(
            {
                "folder": meta.folder,
                "name": meta.name,
                "workshop_id": meta.workshop_id,
                "status": status,
                "last_updated": last_updated,
            }
        )

    started = False
    if start_refresh and (stale or len(rows) < len(metas)):
        started = refresh_async(mods_path, metas)

    return {
        "mods": results,
        "stale": stale,
        "refreshing": started or _is_refreshing(_key(mods_path)),
    }


def _read_rows(workshop_ids: list[str]) -> dict[str, tuple[int, int | None]]:
    """Return ``{workshop_id: (time_updated, age_seconds)}`` from mod_cache.

    Chunked at 500 ids: SQLite's traditional host-parameter limit is 999 and
    a 600+ mod setup must not degrade into a cache miss on every request.
    """
    if not workshop_ids:
        return {}
    result: dict[str, tuple[int, int | None]] = {}
    db = get_conn()
    for start in range(0, len(workshop_ids), 500):
        chunk = workshop_ids[start : start + 500]
        marks = ",".join("?" for _ in chunk)
        sql = (
            f"SELECT workshop_id, time_updated, "  # nosec B608 — marks is '?' placeholders only
            f"CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', cached_at) AS INTEGER) "
            f"AS age_s FROM mod_cache WHERE workshop_id IN ({marks})"
        )
        try:
            rows = db.execute(sql, chunk).fetchall()
        except Exception:  # noqa: BLE001 — a broken cache must degrade, not 500
            _log.warning("健康缓存读取失败，按无缓存处理", exc_info=True)
            return result
        for row in rows:
            result[row["workshop_id"]] = (
                int(row["time_updated"] or 0),
                None if row["age_s"] is None else int(row["age_s"]),
            )
    return result


# ── refresh path ──────────────────────────────────────────────────


def refresh_async(mods_dir: Path, metas: list[ModMeta] | None = None) -> bool:
    """Start a background refresh unless one is already running for this dir.

    Returns True when this call started the worker.
    """
    mods_path = Path(mods_dir)
    key = _key(mods_path)
    with _lock:
        if key in _refreshing:
            return False
        thread = threading.Thread(
            target=_refresh_worker,
            args=(key, mods_path, metas),
            name=f"rwmod-health-{mods_path.name}",
            daemon=True,
        )
        _refreshing[key] = thread
    try:
        thread.start()
    except RuntimeError:  # pragma: no cover — interpreter shutting down
        with _lock:
            _refreshing.pop(key, None)
        return False
    return True


def refresh_now(mods_dir: Path, metas: list[ModMeta] | None = None) -> bool:
    """Run a refresh synchronously (tests / explicit maintenance).

    Returns True when at least one batch succeeded and the cache was written.
    """
    mods_path = Path(mods_dir)
    key = _key(mods_path)
    with _lock:
        existing = _refreshing.get(key)
    if existing is not None and existing is not threading.current_thread():
        existing.join(_JOIN_TIMEOUT)
    return _refresh(mods_path, metas)


def wait_for_refresh(mods_dir: Path, timeout: float = 10.0) -> bool:
    """Join the in-flight refresh; True when none is running afterwards."""
    key = _key(Path(mods_dir))
    with _lock:
        thread = _refreshing.get(key)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
    with _lock:
        return key not in _refreshing


def stop_all(timeout: float = 10.0) -> None:
    """Join every background refresh (daemon threads would not block exit)."""
    with _lock:
        threads = list(_refreshing.values())
    deadline = time.monotonic() + max(0.0, timeout)
    for thread in threads:
        remaining = deadline - time.monotonic()
        if thread.is_alive() and remaining > 0:
            thread.join(remaining)


def _is_refreshing(key: str) -> bool:
    with _lock:
        return key in _refreshing


def _refresh_worker(key: str, mods_dir: Path, metas: list[ModMeta] | None) -> None:
    try:
        _refresh(mods_dir, metas)
    except Exception:  # noqa: BLE001 — background work must never crash the server
        _log.warning("健康检查后台刷新失败", exc_info=True)
    finally:
        with _lock:
            if _refreshing.get(key) is threading.current_thread():
                _refreshing.pop(key, None)


def _refresh(mods_dir: Path, metas: list[ModMeta] | None = None) -> bool:
    """Fetch remote details per batch and persist successful batches only."""
    if metas is None:
        metas = get_cached_mods(mods_dir)
    ids = [m.workshop_id for m in metas if m.workshop_id]
    if not ids:
        return False

    batches = [ids[i : i + _BATCH_SIZE] for i in range(0, len(ids), _BATCH_SIZE)]
    details: dict[str, dict] = {}
    covered: list[str] = []
    failed = 0

    max_workers = min(len(batches), _MAX_WORKERS)
    with ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="rwmod-health-fetch"
    ) as pool:
        futures = {pool.submit(_fetch_batch, batch): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                response = future.result()
            except Exception:  # noqa: BLE001 — one batch failure is not fatal
                failed += 1
                continue
            # Empty response for a non-empty batch means the request failed
            # (Steam returns an entry, with result=0, for removed items).
            if not response:
                failed += 1
                continue
            covered.extend(batch)
            details.update(response)

    if not covered:
        _log.warning("健康检查刷新失败（%d/%d 批），保留旧缓存", failed, len(batches))
        return False

    rows: list[tuple[str, int]] = []
    for workshop_id in covered:
        remote = details.get(workshop_id)
        if remote is None:
            rows.append((workshop_id, _REMOVED_SENTINEL))
        else:
            rows.append((workshop_id, int(remote.get("time_updated", 0) or 0)))

    try:
        db = get_conn()
        with db:
            db.executemany(
                """INSERT INTO mod_cache
                       (workshop_id, title, author, description, time_updated, cached_at)
                   VALUES (?, '', '', '', ?, datetime('now'))
                   ON CONFLICT(workshop_id) DO UPDATE SET
                       time_updated = excluded.time_updated,
                       cached_at = datetime('now')""",
                rows,
            )
    except Exception:  # noqa: BLE001 — cache write failure must not break the app
        _log.warning("健康缓存写入失败", exc_info=True)
        return False

    _log.debug("健康检查刷新完成: %d 个 mod，%d 批失败", len(rows), failed)
    return True
