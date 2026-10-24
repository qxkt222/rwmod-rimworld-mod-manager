"""Mod metadata cache — avoids re-parsing About.xml on every request.

Uses a TTL-based in-memory cache with directory mtime-based invalidation.
When a mod folder is touched (installed/deleted), only that entry is invalidated.

The in-memory layer is partitioned by ``str(mods_dir)``: switching to another
instance (another mods directory) must never serve the previous instance's
metadata, even when both contain a folder with the same name.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from rwmod.metadata import ModMeta, read_mod_metadata

__all__ = ["clear_memory_cache", "get_cached_mods", "invalidate_cache"]

_MAX_AGE_SECONDS = 30  # TTL: re-scan entire mods dir after this many seconds
#: mods dir (str) → (folder → meta, cached_at monotonic, dir mtime)
_cache: dict[str, tuple[dict[str, ModMeta], float, float]] = {}
_lock = threading.Lock()


def get_cached_mods(mods_dir: Path, instance_id: str | None = None) -> list[ModMeta]:
    """Return cached mod metadata with three-layer fallback.

    1. In-memory TTL cache (30s) — instant for rapid re-requests.
    2. SQLite persistent cache — per-mod incremental refresh, survives restarts.
    3. Full filesystem scan — only when cache is cold (first run).
    """
    key = str(mods_dir)
    if not mods_dir.exists():
        with _lock:
            _cache.pop(key, None)
        return []

    dir_mtime = _mods_dir_mtime(mods_dir)
    now = time.monotonic()

    # Layer 1: in-memory cache hit (per mods directory)
    with _lock:
        part = _cache.get(key)
        if part is not None:
            cached, cached_at, cached_mtime = part
            if cached and (now - cached_at) < _MAX_AGE_SECONDS and dir_mtime == cached_mtime:
                return list(cached.values())

    # Layer 2: persistent SQLite cache with per-mod incremental refresh.
    # get_or_refresh_metas handles cold start internally — if DB is empty,
    # it does a full scan and writes results back to SQLite.
    # Layer 3 (below) is a fallback only if the DB layer is completely broken.
    try:
        from rwmod.cache_db import get_or_refresh_metas

        if instance_id is None:
            # Let cache_db derive the owning instance from mods_dir; this also
            # keeps the long-standing one-argument call shape.
            metas = get_or_refresh_metas(mods_dir)
        else:
            metas = get_or_refresh_metas(mods_dir, instance_id=instance_id)
    except Exception:
        _log = logging.getLogger(__name__)
        _log.warning("持久化缓存不可用，回退到全量扫描", exc_info=True)
        metas = []

    if metas:
        with _lock:
            _cache[key] = ({m.folder: m for m in metas}, now, dir_mtime)
        return metas

    # Layer 3: full filesystem scan (emergency fallback — only if SQLite cache is
    # completely broken/unavailable). Normal operation never reaches this path
    # because get_or_refresh_metas does a full scan when DB is empty.
    new_cache: dict[str, ModMeta] = {}
    try:
        entries_iter = sorted(os.scandir(mods_dir), key=lambda e: e.name)
    except OSError:
        return []
    for entry in entries_iter:
        # A mod folder may vanish mid-scan (concurrent download/delete) —
        # one bad entry must not 500 every listing endpoint.
        try:
            if not entry.is_dir():
                continue
            meta = read_mod_metadata(Path(entry.path))
            if meta:
                new_cache[meta.folder] = meta
        except OSError:
            continue

    metas_full = list(new_cache.values())
    with _lock:
        _cache[key] = (new_cache, now, dir_mtime)
    return metas_full


def clear_memory_cache() -> None:
    """Drop the whole in-memory layer (used when switching instances)."""
    with _lock:
        _cache.clear()


def invalidate_cache(folder: str | None = None) -> None:
    """Invalidate a specific folder entry or the entire cache.

    Args:
        folder: mod folder name to invalidate, or None for full invalidation.
    """
    with _lock:
        if folder is None:
            _cache.clear()
        else:
            for cached, _cached_at, _mtime in _cache.values():
                cached.pop(folder, None)

    # Also invalidate the persistent cache
    try:
        from rwmod.cache_db import invalidate_all, invalidate_folder

        if folder is None:
            invalidate_all()
        else:
            invalidate_folder(folder)
    except Exception:
        pass


def _mods_dir_mtime(mods_dir: Path) -> float:
    """Get the latest mtime of the mods directory or any mod subdirectory.

    Uses directory modification time as a coarse invalidation signal.
    A new mod folder or deletion changes the parent dir's mtime, but editing a
    mod's About.xml only touches the *child* dir — so we must also consider
    child mtimes, otherwise the in-memory cache can serve stale metadata after
    a mod update (e.g. a changed display name or packageId) within the TTL.
    """
    try:
        latest = mods_dir.stat().st_mtime
    except OSError:
        return 0
    try:
        with os.scandir(mods_dir) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    if mtime > latest:
                        latest = mtime
    except OSError:
        pass
    return latest
