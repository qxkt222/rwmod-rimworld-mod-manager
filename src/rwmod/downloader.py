"""Download + retry + copy-to-mods logic — with smart error handling."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import deque
from pathlib import Path

from rwmod.config import Config
from rwmod.database import find_local_mods_by_workshop_id
from rwmod.steamcmd import DownloadResult, ErrorKind, SteamCMD
from rwmod.utils import extract_mod_id, safe_filename
from rwmod.xmlutil import parse_about_field


def _try_broadcast() -> None:
    """Notify WebSocket clients of queue changes, if server is running.

    The WebSocket endpoint in server.py is a lightweight echo/ping handler
    without a connection registry, so real broadcasts are not implemented yet.
    This is intentionally a no-op hook for future use.
    """


__all__ = ["download_one", "extract_mod_id", "_find_existing", "_pick_folder_name"]

_log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 5

# Per-mod locks: concurrent downloads of the same mod are serialized so the
# check-then-download sequence is atomic. Prevents TOCTOU races where two
# workers download the same mod and corrupt each other.
_mod_locks: dict[str, threading.Lock] = {}
_mod_locks_guard = threading.Lock()


# Cap on the per-mod lock dict so a long-running server (thousands of
# distinct workshop IDs) does not grow it without bound.
_MOD_LOCKS_MAX = 1024


def _lock_for(mod_id: str) -> threading.Lock:
    """Return (creating if needed) the per-mod lock."""
    with _mod_locks_guard:
        lock = _mod_locks.get(mod_id)
        if lock is None:
            lock = threading.Lock()
            _mod_locks[mod_id] = lock
            # Drop locks nobody is holding or waiting on when the dict grows
            # past the cap. locked() is True while a thread holds OR waits on
            # the lock, so in-use locks are never collected.
            if len(_mod_locks) > _MOD_LOCKS_MAX:
                stale = [k for k, lock2 in _mod_locks.items() if not lock2.locked()]
                for k in stale:
                    _mod_locks.pop(k, None)
                    if len(_mod_locks) <= _MOD_LOCKS_MAX // 2:
                        break
        return lock


def _find_existing(mods_dir: Path, mod_id: str) -> Path | None:
    """Find an installed mod folder by workshop ID.

    Fast path: query the local_mod_metadata table via its indexed
    workshop_id column. Avoids a full directory scan on every download.

    Fallback: the metadata table may lag behind the filesystem (e.g. a mod
    folder copied in by hand). In that case fall back to a one-time scan and
    repair the metadata table so future lookups stay fast.
    """
    if not mods_dir.exists():
        return None

    try:
        folders = find_local_mods_by_workshop_id(mod_id)
        for folder in folders:
            cand = mods_dir / folder
            if cand.is_dir():
                return cand
    except Exception:  # noqa: BLE001 - DB issues must not break downloads
        _log.warning("数据库查找 mod %s 失败，回退目录扫描", mod_id)

    found: Path | None = None
    try:
        for d in mods_dir.iterdir():
            if not d.is_dir():
                continue
            pf = d / "About" / "PublishedFileId.txt"
            try:
                if pf.exists() and pf.read_text(encoding="utf-8").strip() == mod_id:
                    found = d
                    break
            except OSError:
                continue
    except OSError as e:
        _log.warning("扫描 %s 失败: %s", mods_dir, e)
        return None

    if found is not None:
        try:
            from rwmod.database import upsert_local_mod_workshop_id

            upsert_local_mod_workshop_id(found.name, mod_id)
        except Exception:  # noqa: BLE001
            pass
    return found


def _read_about_field(mod_dir: Path, tag: str) -> str:
    about = mod_dir / "About" / "About.xml"
    if about.exists():
        try:
            return parse_about_field(about, tag) or ""
        except Exception:
            pass
    return ""


def _pick_folder_name(workshop_path: Path, mod_id: str) -> str:
    pkg = _read_about_field(workshop_path, "packageId")
    if pkg:
        return safe_filename(pkg, allow_empty=False)
    name = _read_about_field(workshop_path, "name")
    if name:
        return f"{safe_filename(name, allow_empty=False)}_{mod_id}"
    return f"mod_{mod_id}"


def download_one(config: Config, mod_id: str, force: bool = False) -> bool:
    """Download a single mod. Returns True on success.

    Serializes concurrent downloads of the same mod via a per-mod lock,
    making the check-then-install sequence (find existing copy) atomic.

    Collection children and dependencies are collected while the lock is
    held but downloaded AFTER it is released — holding a per-mod lock across
    recursive downloads of *other* mods can deadlock two threads that lock
    in opposite order (A locks A, wants B; B locks B, wants A).
    """
    deferred: list[str] = []
    with _lock_for(mod_id):
        ok = _download_one_unlocked(config, mod_id, force, deferred)
    for pid in deferred:
        _log.info("下载子项/依赖（锁外）: %s", pid)
        download_one(config, pid, force)
    return ok


def _download_one_unlocked(
    config: Config,
    mod_id: str,
    force: bool = False,
    deferred: list[str] | None = None,
) -> bool:
    """Core download logic. Caller must already hold the per-mod lock.

    ``deferred`` (when given) receives IDs of collection children /
    dependencies that still need downloading; the caller processes them
    after releasing the lock.
    """
    steamcmd = SteamCMD(config.steamcmd_path)

    old_backed_up = False
    existing = _find_existing(config.mods_dir, mod_id)
    if existing:
        if force:
            _log.info("覆盖前备份: %s", existing.name)
            try:
                from rwmod.backup import backup_mod

                if backup_mod(config.mods_dir, mod_id, existing.name, config.backup_dir):
                    old_backed_up = True
            except Exception as e:
                _log.warning("备份失败: %s", e)
            shutil.rmtree(existing)
        else:
            _log.info("已安装: %s", existing.name)
            return True

    # ── collection detection (Web API, no SteamCMD needed) ──────────
    # SteamCMD cannot download collections — only individual mod files.
    # Detect collections via Steam Web API first and handle them separately.
    try:
        from rwmod.workshop import fetch_collection_children, is_collection

        if is_collection(mod_id):
            _log.info("检测到合集 ID: %s，获取子项...", mod_id)
            children = fetch_collection_children(mod_id)
            if not children:
                _log.warning("合集 %s 为空或无法获取子项", mod_id)
                return False
            _log.info("合集包含 %s 个 Mod，交给锁外队列下载...", len(children))
            if deferred is None:
                # 无收集器（例如直接调用）时保持原行为：逐个递归下载
                ok = 0
                for i, cid in enumerate(children, 1):
                    _log.info("[%s/%s] 合集子项 %s", i, len(children), cid)
                    if download_one(config, cid, force=force):
                        ok += 1
                    _try_broadcast()
                return ok > 0
            for cid in children:
                if force or not _find_existing(config.mods_dir, cid):
                    deferred.append(cid)
            return bool(deferred)
    except Exception as e:
        # If collection detection fails (network issue), fall through to
        # regular SteamCMD download — it will fail with a clear error too.
        _log.debug("合集检测失败（将尝试常规下载）: %s", e)

    last_result: DownloadResult | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        _log.info("尝试 %s/%s...", attempt, MAX_RETRIES)
        result = steamcmd.workshop_download(mod_id)
        last_result = result

        # Log relevant output lines
        for line in result.output_lines:
            low = line.lower()
            if any(kw in low for kw in ("download", "success", "error", "fail", "item")):
                _log.info("  %s", line)

        if result.success:
            break

        # Permanent errors → no point retrying
        if result.error_kind in ErrorKind.NON_RETRYABLE:
            _log.warning(
                "不可重试错误 [%s]: %s",
                result.error_kind,
                result.error_detail or ErrorKind.explain(result.error_kind),
            )
            break

        # Transient errors → retry
        if attempt < MAX_RETRIES:
            _log.warning("SteamCMD 错误 [%s]，%ss 后重试...", result.error_kind, RETRY_DELAY)
            time.sleep(RETRY_DELAY)

    # ── handle successful download ───────────────────────────────
    if last_result and last_result.success:
        workshop_dir = steamcmd.workshop_content_dir / mod_id
        if not workshop_dir.exists():
            _log.warning("未找到下载内容: %s", mod_id)
            return _skymods_fallback(config, mod_id)

        # Check if this is a collection
        about = workshop_dir / "About" / "About.xml"
        if not about.exists():
            _log.warning("Downloaded content is not a valid mod (no About.xml): %s", mod_id)
            return _skymods_fallback(config, mod_id)

        folder_name = _pick_folder_name(workshop_dir, mod_id)
        dest = config.mods_dir / folder_name
        if dest.exists() and not force:
            _log.info("目标已存在: %s", dest)
            return True
        if dest.exists() and force:
            shutil.rmtree(dest)

        try:
            shutil.copytree(workshop_dir, dest)
            (dest / ".rwmod_last_updated").write_text(str(int(time.time())))
        except OSError as e:
            _log.error("复制下载内容失败: %s", e)
            if old_backed_up:
                _auto_restore_backup(config, mod_id)
            return False
        _log.info("下载完成: %s", folder_name)

        # Auto-download dependencies (collected, downloaded outside the lock)
        _collect_deps(config, mod_id, deferred)
        return True

    # ── handle failure ───────────────────────────────────────────
    if last_result:
        err_msg = last_result.error_detail or ErrorKind.explain(last_result.error_kind)
        _log.error("下载失败: %s — %s", mod_id, err_msg)

    # Only try Skymods if the error is potentially recoverable
    if (
        last_result
        and last_result.error_kind not in ErrorKind.NON_RETRYABLE
        and _skymods_fallback(config, mod_id)
    ):
        return True

    # For non-retryable errors, explain clearly and skip Skymods
    if last_result:
        _log.error(
            "❌ %s (%s): %s — 已跳过 Skymods（错误不可恢复）",
            mod_id,
            last_result.error_kind,
            err_msg,
        )
    else:
        _log.error("❌ %s: 下载失败", mod_id)

    # force 覆盖时旧版已被删除——下载失败则自动从备份回滚，避免丢 mod。
    if old_backed_up:
        _auto_restore_backup(config, mod_id)
    return False


def _auto_restore_backup(config: Config, mod_id: str) -> None:
    """Roll back to the most recent backup after a failed force reinstall."""
    try:
        from rwmod.backup import restore_mod

        result = restore_mod(config.mods_dir, mod_id, config.backup_dir)
        if result.get("ok"):
            _log.info("下载失败，已自动回滚备份: %s", mod_id)
        else:
            _log.warning("下载失败且自动回滚失败: %s", result.get("msg"))
    except Exception as e:  # noqa: BLE001 — rollback must never mask the error
        _log.warning("下载失败后自动回滚异常: %s", e)


def _skymods_fallback(config: Config, mod_id: str) -> bool:
    """Try Skymods as fallback source."""
    _log.info("尝试 Skymods 备用源...")
    from rwmod.skymods import try_skymods

    result = try_skymods(mod_id, config)
    if result:
        _log.info("Skymods 备用源下载成功: %s → %s", mod_id, result)
        return True
    _log.error("Skymods 备用源也失败: %s", mod_id)
    return False


def _collect_deps(config: Config, mod_id: str, deferred: list[str] | None) -> None:
    """Collect not-yet-installed dependencies of a freshly installed mod.

    Uses a breadth-first traversal so transitive dependencies (a dependency's
    own dependencies) are also fetched, not just the direct ones. A visited
    set prevents infinite loops on cyclic dependency graphs.

    Collected IDs are appended to ``deferred`` and downloaded by the caller
    AFTER the per-mod lock is released (see download_one).
    """
    try:
        from rwmod.workshop import fetch_item_dependencies

        queue: deque[str] = deque([mod_id])
        visited: set[str] = set()
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            deps = fetch_item_dependencies([current])
            for dep_id in deps.get(current, []):
                if dep_id in visited:
                    continue
                if _find_existing(config.mods_dir, dep_id):
                    visited.add(dep_id)
                    continue
                _log.info("收集依赖: %s", dep_id)
                if deferred is not None:
                    deferred.append(dep_id)
                visited.add(dep_id)
                queue.append(dep_id)
    except Exception as e:
        _log.warning("依赖检测失败: %s", e)
