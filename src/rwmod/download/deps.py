"""Dependency collection for a freshly installed mod."""

from __future__ import annotations

import logging
from collections import deque

from rwmod.config import Config

_log = logging.getLogger(__name__)


def _collect_deps(config: Config, mod_id: str, deferred: list[str] | None) -> None:
    """Collect not-yet-installed dependencies of a freshly installed mod.

    Uses a breadth-first traversal so transitive dependencies (a dependency's
    own dependencies) are also fetched, not just the direct ones. A visited
    set prevents infinite loops on cyclic dependency graphs.

    The Steam API query is done **per BFS level** (fetch_item_dependencies
    accepts many ids in one GetPublishedFileDetails call), so a mod with
    dozens of dependencies costs a handful of requests instead of one per
    dependency.

    Collected IDs are appended to ``deferred`` and downloaded by the caller
    AFTER the per-mod lock is released (see download_one).
    """
    try:
        # Resolved through the historical module so monkeypatching
        # ``rwmod.downloader.find_local_mods_by_workshop_id`` still reaches
        # the lookup used here.
        from rwmod import downloader as _dl
        from rwmod.workshop import fetch_item_dependencies

        queue: deque[str] = deque([mod_id])
        queued: set[str] = {mod_id}  # ids already queued / decided (dedupe)
        while queue:
            # Drain the whole current level, then query it in ONE API call.
            level: list[str] = []
            while queue:
                current = queue.popleft()
                level.append(current)
            if not level:
                continue
            deps_map = fetch_item_dependencies(level)
            for current in level:
                for dep_id in deps_map.get(current, []):
                    if dep_id in queued:
                        continue
                    if _dl._find_existing(
                        config.mods_dir, dep_id, instance_id=_dl._instance_scope(config)
                    ):
                        queued.add(dep_id)
                        continue
                    _log.info("收集依赖: %s", dep_id)
                    if deferred is not None:
                        deferred.append(dep_id)
                    queued.add(dep_id)
                    queue.append(dep_id)
    except Exception as e:
        _log.warning("依赖检测失败: %s", e)
