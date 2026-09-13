"""Download + retry + copy-to-mods logic — with smart error handling."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rwmod.config import Config
from rwmod.database import find_local_mods_by_workshop_id, find_local_mods_by_workshop_ids
from rwmod.steamcmd import DownloadResult, ErrorKind, SteamCMD
from rwmod.utils import extract_mod_id, safe_filename
from rwmod.xmlutil import parse_about_field

__all__ = [
    "download_one",
    "download_batch",
    "extract_mod_id",
    "_find_existing",
    "_pick_folder_name",
]

_log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 5

# Batch mode: one SteamCMD process downloads several mods back-to-back (the
# per-mod login/startup overhead is paid once per batch instead of per mod).
BATCH_SIZE = 6
# Copy / Skymods-fallback workers used after a batch download finishes.
_BATCH_WORKERS = 4

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

    return _scan_find_existing(mods_dir, {mod_id}).get(mod_id)


def _find_existing_many(mods_dir: Path, mod_ids: list[str]) -> dict[str, Path | None]:
    """Batch variant of _find_existing — one DB query, one fallback scan.

    Used by the batch downloader: checking N mods with _find_existing would
    issue N DB queries and — when the metadata table lacks a row — N full
    directory scans. Here a single directory scan indexes every
    PublishedFileId.txt at once and repairs the metadata table.
    """
    if not mods_dir.exists() or not mod_ids:
        return {mid: None for mid in mod_ids}

    result: dict[str, Path | None] = {}
    missing: list[str] = []
    try:
        by_id = find_local_mods_by_workshop_ids(mod_ids)
        for mid in mod_ids:
            folder = next((f for f in by_id.get(mid, []) if (mods_dir / f).is_dir()), None)
            if folder is not None:
                result[mid] = mods_dir / folder
            else:
                missing.append(mid)
    except Exception:  # noqa: BLE001 - DB issues must not break downloads
        missing = list(mod_ids)

    if missing:
        result.update(_scan_find_existing(mods_dir, set(missing)))
    return result


def _scan_find_existing(mods_dir: Path, mod_ids: set[str]) -> dict[str, Path | None]:
    """One directory scan resolving PublishedFileId.txt → folder for many ids.

    Repairs the metadata table for hits so future lookups are DB-only.
    """
    found: dict[str, Path | None] = {mid: None for mid in mod_ids}
    hits: list[tuple[str, str]] = []  # (folder, workshop_id) to persist
    try:
        for d in mods_dir.iterdir():
            if not d.is_dir():
                continue
            pf = d / "About" / "PublishedFileId.txt"
            try:
                if pf.exists():
                    wid = pf.read_text(encoding="utf-8").strip()
                    if wid in mod_ids and found[wid] is None:
                        found[wid] = d
                        hits.append((d.name, wid))
            except OSError:
                continue
    except OSError as e:
        _log.warning("扫描 %s 失败: %s", mods_dir, e)
        return found

    if hits:
        try:
            from rwmod.database import upsert_local_mod_workshop_id

            for folder, wid in hits:
                upsert_local_mod_workshop_id(folder, wid)
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


def _install_content(config: Config, mod_id: str, force: bool = False) -> bool:
    """Copy SteamCMD's downloaded workshop content into the mods directory.

    Returns True when the mod ended up installed. Failures are logged and
    returned as False so the caller can fall back (Skymods / restore).
    """
    steamcmd = SteamCMD(config.steamcmd_path)
    workshop_dir = steamcmd.workshop_content_dir / mod_id
    if not workshop_dir.exists():
        _log.warning("未找到下载内容: %s", mod_id)
        return False

    # Check if this is a valid mod (has About.xml)
    about = workshop_dir / "About" / "About.xml"
    if not about.exists():
        _log.warning("Downloaded content is not a valid mod (no About.xml): %s", mod_id)
        return False

    folder_name = _pick_folder_name(workshop_dir, mod_id)
    dest = config.mods_dir / folder_name
    if dest.exists() and not force:
        _log.info("目标已存在: %s", dest)
        return True
    if dest.exists() and force:
        shutil.rmtree(dest)

    try:
        shutil.copytree(workshop_dir, dest)
        # Record the install time in the metadata DB (replaces the old
        # per-mod .rwmod_last_updated marker file, which polluted the game
        # directory and failed on read-only mounts). Best effort — a DB
        # hiccup must never fail an otherwise-successful install.
        try:
            from rwmod.database import set_last_updated

            set_last_updated(folder_name, int(time.time()))
        except Exception:  # noqa: BLE001 — timestamp is auxiliary data
            _log.debug("写入安装时间戳失败: %s", folder_name)
    except OSError as e:
        _log.error("复制下载内容失败: %s", e)
        return False
    return True


def download_batch(
    config: Config,
    mod_ids: list[str],
    force: bool = False,
    extra_out: list[str] | None = None,
    progress_cb: Callable[[str, float, float, float], None] | None = None,
) -> dict[str, bool]:
    """Download several mods in ONE SteamCMD process — the fast path.

    The per-mod login/startup overhead of SteamCMD (several seconds per
    process) is paid once for the whole batch, and SteamCMD runs the item
    downloads back-to-back in a single session.

    - Already-installed ids (unless ``force``) count as success and are skipped.
    - Collection ids are detected up front; their children are appended to
      ``extra_out`` (the caller re-queues them) instead of being downloaded
      via SteamCMD (which cannot download collections).
    - Items that fail the batch download are retried individually via
      ``download_one`` (which has its own retry + Skymods fallback).

    Returns a dict {workshop_id: success}. ``extra_out`` collects ids that
    still need downloading (collection children / new dependencies).
    """
    out: dict[str, bool] = {mid: True for mid in dict.fromkeys(mod_ids)}
    unique_ids = list(dict.fromkeys(mod_ids))
    if not force:
        existing = _find_existing_many(config.mods_dir, unique_ids)
        todo = [mid for mid in unique_ids if existing.get(mid) is None]
    else:
        todo = unique_ids
    if not todo:
        return out

    # ── collections: detect up front, hand children to the caller ──
    children: list[str] = []
    try:
        from rwmod.workshop import fetch_collection_children, is_collection

        def _collection_children(mid: str) -> list[str] | None:
            try:
                if is_collection(mid):
                    return fetch_collection_children(mid)
            except Exception:  # noqa: BLE001 — detection must not break the batch
                return None
            return None

        with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as ex:
            collection_results = list(ex.map(_collection_children, todo))
        for mid, kids in zip(todo, collection_results, strict=True):
            if kids:
                _log.info("合集 ID %s：%s 个子项交给队列下载", mid, len(kids))
                children.extend(kids)
                out[mid] = True  # collection itself is not a mod
        if children:
            if extra_out is not None:
                extra_out.extend(children)
            else:
                # No collector (direct call): keep the old recursive behavior.
                for cid in children:
                    download_one(config, cid, force)
            todo = [mid for mid, kids in zip(todo, collection_results, strict=True) if not kids]
            if not todo:
                return out
    except Exception as e:  # noqa: BLE001 — collection detection must not kill the batch
        _log.debug("合集检测失败（继续批量下载）: %s", e)

    # ── batch SteamCMD download (one process, one login) ──────────
    steamcmd = SteamCMD(config.steamcmd_path)
    results = steamcmd.workshop_download_many(todo, progress_cb=progress_cb)
    # Defensive: every requested id must have a result entry.
    for mid in todo:
        if mid not in results:
            _log.error("批量下载缺少 %s 的结果（进程异常退出）", mid)
            out[mid] = False

    # ── install downloaded content in parallel ───────────────────
    ok_ids = [mid for mid, r in results.items() if r.success]

    def _install(mid: str) -> bool:
        with _lock_for(mid):
            return _install_content(config, mid, force)

    installed: dict[str, bool] = {}
    if ok_ids:
        with ThreadPoolExecutor(max_workers=min(_BATCH_WORKERS, max(1, len(ok_ids)))) as ex:
            installed = dict(zip(ok_ids, ex.map(_install, ok_ids), strict=True))
        for mid in ok_ids:
            if installed[mid]:
                _collect_deps(config, mid, extra_out)
            else:
                _log.warning("安装失败（内容已下载），尝试 Skymods: %s", mid)
                out[mid] = _skymods_fallback(config, mid)

    # ── failures: retry individually (retry loop + Skymods) ──────
    for mid, r in results.items():
        if r.success:
            out[mid] = installed.get(mid, True)
            continue
        if r.error_kind in ErrorKind.NON_RETRYABLE:
            _log.error(
                "❌ %s (%s): %s — 已跳过 Skymods（错误不可恢复）",
                mid,
                r.error_kind,
                r.error_detail or ErrorKind.explain(r.error_kind),
            )
            out[mid] = False
            continue
        # Recoverable failure → per-mod retry path (own retries + Skymods).
        out[mid] = download_one(config, mid, force)
    return out


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

    # Force-overwrite: the existing mod is renamed aside (not deleted) so a
    # failed download can roll it back. Deleting first (the old rmtree) left
    # a window where a failed download with no backup permanently lost the mod.
    old_backed_up = False
    existing = _find_existing(config.mods_dir, mod_id)
    stashed: Path | None = None
    if existing:
        if force:
            _log.info("覆盖前暂存旧版: %s", existing.name)
            stashed = config.mods_dir / (
                f".rwmod_stash_{safe_filename(existing.name, allow_empty=False)}_{int(time.time())}"
            )
            try:
                existing.rename(stashed)
                old_backed_up = True
            except OSError as e:
                _log.warning("暂存旧版失败（将以备份方式覆盖）: %s", e)
                try:
                    from rwmod.backup import backup_mod

                    if backup_mod(config.mods_dir, mod_id, existing.name, config.backup_dir):
                        old_backed_up = True
                except Exception as e2:  # noqa: BLE001
                    _log.warning("备份失败: %s", e2)
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
        if not _install_content(config, mod_id, force):
            _log.warning("安装下载内容失败，尝试 Skymods: %s", mod_id)
            return _skymods_fallback(config, mod_id)
        _log.info("下载完成: %s", mod_id)
        # Force-overwrite succeeded — the stashed old copy is now obsolete.
        _discard_stash(stashed)

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
        # Skymods installed a copy — the force-overwrite stash is obsolete.
        _discard_stash(stashed)
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

    # force 覆盖时旧版已被移开——下载失败则从暂存目录（或备份）自动回滚，避免丢 mod。
    if old_backed_up:
        if _rollback_stash(config, mod_id, stashed):
            pass
        else:
            _auto_restore_backup(config, mod_id)
    return False


def _rollback_stash(config: Config, mod_id: str, stashed: Path | None) -> bool:
    """Put a force-overwrite's stashed old copy back into place.

    Returns True if the stash existed and was restored (or the mod was
    already re-installed by the failed download's partial copy).
    """
    if stashed is None or not stashed.exists():
        return False
    dest = config.mods_dir / stashed.name[len(".rwmod_stash_") :].rsplit("_", 1)[0]
    if dest.exists():
        # A partial copy landed in the final location — drop the stash, the
        # download did install something.
        _discard_stash(stashed)
        return True
    try:
        stashed.rename(dest)
        _log.info("下载失败，已自动回滚旧版: %s", dest.name)
        return True
    except OSError as e:
        _log.warning("下载失败且回滚暂存目录失败: %s", e)
        return False


def _discard_stash(stashed: Path | None) -> None:
    """Remove a force-overwrite stash once it is obsolete."""
    if stashed is None:
        return
    try:
        if stashed.exists():
            shutil.rmtree(stashed)
    except OSError as e:
        _log.warning("清理暂存目录失败: %s", e)


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

    The Steam API query is done **per BFS level** (fetch_item_dependencies
    accepts many ids in one GetPublishedFileDetails call), so a mod with
    dozens of dependencies costs a handful of requests instead of one per
    dependency.

    Collected IDs are appended to ``deferred`` and downloaded by the caller
    AFTER the per-mod lock is released (see download_one).
    """
    try:
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
                    if _find_existing(config.mods_dir, dep_id):
                        queued.add(dep_id)
                        continue
                    _log.info("收集依赖: %s", dep_id)
                    if deferred is not None:
                        deferred.append(dep_id)
                    queued.add(dep_id)
                    queue.append(dep_id)
    except Exception as e:
        _log.warning("依赖检测失败: %s", e)
