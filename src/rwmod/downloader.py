"""Download + retry + copy-to-mods logic — with smart error handling.

The implementation is organised under :mod:`rwmod.download` (outcomes, locks,
install/rollback helpers, dependency collection, the single-mod state
machine). This module keeps the historical ``rwmod.downloader`` import surface
— every name below is re-exported — and owns the two orchestrators plus the
helpers that callers and tests patch at this path (``SteamCMD``, discovery,
``_install_content``, ``_skymods_fallback``).
"""

from __future__ import annotations

import logging
import random
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rwmod.config import Config
from rwmod.database import find_local_mods_by_workshop_id, find_local_mods_by_workshop_ids
from rwmod.download.core import _download_one_unlocked
from rwmod.download.deps import _collect_deps
from rwmod.download.install import (
    _auto_restore_backup,
    _cleanup_update_backups,
    _discard_stash,
    _persistent_backup,
    _pick_folder_name,
    _read_about_field,
    _rollback_failed_install,
    _rollback_stash,
    _stash_for_overwrite,
    _verify_install,
)
from rwmod.download.locks import (
    _dest_lock_for,
    _lock_for,
    _mod_locks,
    _mod_locks_guard,
)
from rwmod.download.outcomes import (
    KIND_COLLECTION_EMPTY,
    KIND_COLLECTION_PROBE_FAILED,
    KIND_INSTALL_FAILED,
    BatchResult,
    DownloadOutcome,
    explain_outcome,
)
from rwmod.steamcmd import ErrorKind, SteamCMD

__all__ = [
    "download_one",
    "download_batch",
    "DownloadOutcome",
    "BatchResult",
    "explain_outcome",
    "_find_existing",
    "_find_existing_many",
    "_pick_folder_name",
    "_read_about_field",
    "_verify_install",
    "_install_content",
    "_stash_for_overwrite",
    "_rollback_stash",
    "_discard_stash",
    "_auto_restore_backup",
    "_rollback_failed_install",
    "_persistent_backup",
    "_cleanup_update_backups",
    "_collect_deps",
    "_classify_collection",
    "_skymods_fallback",
    "_lock_for",
    "_dest_lock_for",
    "_mod_locks",
    "_mod_locks_guard",
    "_download_one_unlocked",
    "_retry_delay",
    "MAX_RETRIES",
    "RETRY_DELAY",
    "RETRY_BACKOFF",
    "RETRY_JITTER",
    "BATCH_SIZE",
    "KIND_INSTALL_FAILED",
    "KIND_COLLECTION_PROBE_FAILED",
    "KIND_COLLECTION_EMPTY",
    # Re-exported so callers/tests patching ``rwmod.downloader.SteamCMD`` keep
    # working after the implementation moved into ``rwmod.download``.
    "SteamCMD",
]

_log = logging.getLogger(__name__)

# Transient SteamCMD failures are retried MAX_RETRIES times *after* the first
# attempt. The wait grows 5s → 15s → 45s with ±20% jitter so a flaky Steam
# endpoint is not hammered in lockstep.
MAX_RETRIES = 3
RETRY_DELAY = 5
RETRY_BACKOFF = 3
RETRY_JITTER = 0.2


def _retry_delay(attempt: int) -> float:
    """Seconds to wait before the next attempt (attempt is 1-based)."""
    base = RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))
    return float(base * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER))


# Batch mode: one SteamCMD process downloads several mods back-to-back (the
# per-mod login/startup overhead is paid once per batch instead of per mod).
BATCH_SIZE = 6
# Copy / Skymods-fallback workers used after a batch download finishes.
_BATCH_WORKERS = 4


def _instance_scope(config: Config) -> str | None:
    """Instance id stamped on this config, for DB scoping (None = legacy default)."""
    iid = getattr(config, "active_instance", None)
    return iid if isinstance(iid, str) and iid else None


def _find_existing(mods_dir: Path, mod_id: str, instance_id: str | None = None) -> Path | None:
    """Find an installed mod folder by workshop ID.

    ``instance_id`` scopes the metadata lookup to the instance that owns
    *mods_dir*. Without it the query follows the process-global active
    instance, which is wrong when the queue drains an item belonging to a
    different installation.

    Fast path: query the local_mod_metadata table via its indexed
    workshop_id column. Avoids a full directory scan on every download.

    Fallback: the metadata table may lag behind the filesystem (e.g. a mod
    folder copied in by hand). In that case fall back to a one-time scan and
    repair the metadata table so future lookups stay fast.
    """
    if not mods_dir.exists():
        return None

    try:
        folders = find_local_mods_by_workshop_id(mod_id, instance_id=instance_id)
        for folder in folders:
            cand = mods_dir / folder
            if cand.is_dir():
                return cand
    except Exception:  # noqa: BLE001 - DB issues must not break downloads
        _log.warning("数据库查找 mod %s 失败，回退目录扫描", mod_id)

    return _scan_find_existing(mods_dir, {mod_id}, instance_id=instance_id).get(mod_id)


def _find_existing_many(
    mods_dir: Path, mod_ids: list[str], instance_id: str | None = None
) -> dict[str, Path | None]:
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
        by_id = find_local_mods_by_workshop_ids(mod_ids, instance_id=instance_id)
        for mid in mod_ids:
            folder = next((f for f in by_id.get(mid, []) if (mods_dir / f).is_dir()), None)
            if folder is not None:
                result[mid] = mods_dir / folder
            else:
                missing.append(mid)
    except Exception:  # noqa: BLE001 - DB issues must not break downloads
        missing = list(mod_ids)

    if missing:
        result.update(_scan_find_existing(mods_dir, set(missing), instance_id=instance_id))
    return result


def _scan_find_existing(
    mods_dir: Path, mod_ids: set[str], instance_id: str | None = None
) -> dict[str, Path | None]:
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
                upsert_local_mod_workshop_id(folder, wid, instance_id=instance_id)
        except Exception:  # noqa: BLE001
            pass
    return found


def _classify_collection(mod_id: str) -> tuple[bool, bool]:
    """Return ``(is_collection, probe_failed)`` for one workshop id.

    ``workshop.is_collection()`` keeps a bool contract and answers False both
    for "not a collection" and for "Steam API unreachable"; the probe side
    channel tells the two apart. All three calls run in this thread because
    the outcome is thread-local.

    The imports are lazy so tests that monkeypatch ``rwmod.workshop`` keep
    working, and so importing the downloader never touches the network.
    """
    from rwmod.workshop import (
        collection_probe_failed,
        is_collection,
        reset_collection_probe,
    )

    reset_collection_probe()
    verdict = is_collection(mod_id)
    return verdict, collection_probe_failed()


def _install_content(
    config: Config,
    mod_id: str,
    force: bool = False,
    detail_out: list[str] | None = None,
) -> bool:
    """Copy SteamCMD's downloaded workshop content into the mods directory.

    Returns True when the mod ended up installed. Failures are logged and
    returned as False so the caller can fall back (Skymods / restore).
    ``detail_out`` receives one short failure reason when the install fails.

    P1-2: a force overwrite takes a persistent zip backup first, and the
    copied folder must pass ``_verify_install`` before it counts as installed
    (or before ``set_last_updated`` is written).

    The whole install runs under a lock keyed by the *destination folder*:
    two workshop ids can share a packageId and therefore the same folder, and
    a workshop-id lock let them delete/copy the shared folder concurrently.
    """

    def _fail(reason: str) -> bool:
        if detail_out is not None:
            detail_out.append(reason)
        return False

    steamcmd = SteamCMD(config.steamcmd_path)
    workshop_dir = steamcmd.workshop_content_dir / mod_id
    if not workshop_dir.exists():
        _log.warning("未找到下载内容: %s", mod_id)
        return _fail("SteamCMD 未生成下载内容")

    # Check if this is a valid mod (has About.xml)
    about = workshop_dir / "About" / "About.xml"
    if not about.exists():
        _log.warning("Downloaded content is not a valid mod (no About.xml): %s", mod_id)
        return _fail("下载内容不是有效 Mod（缺少 About.xml）")

    folder_name = _pick_folder_name(workshop_dir, mod_id)
    dest = config.mods_dir / folder_name

    with _dest_lock_for(dest):
        if dest.exists() and not force:
            _log.info("目标已存在: %s", dest)
            return True

        # Force-overwrite: rename the old copy aside instead of deleting it, so a
        # failed copy below rolls back — the batch path (auto-update included)
        # must not permanently lose the installed version. Same stash contract as
        # the single-mod path in _download_one_unlocked. A persistent zip is taken
        # first so the old version survives even a crash mid-update.
        stashed: Path | None = None
        backup_path: Path | None = None
        if dest.exists():
            backup_path = _persistent_backup(config, mod_id, dest)
            stashed = _stash_for_overwrite(dest)
            if stashed is None:
                if backup_path is None:
                    # No stash, no zip: deleting here would destroy the only
                    # copy of the user's mod. Keep it and report the failure.
                    _log.error("无法暂存旧版且无可用备份，保留旧版并取消安装: %s", dest.name)
                    return _fail("无法暂存旧版且无可用备份，已保留旧版")
                # The persistent zip can restore it if the copy fails.
                try:
                    shutil.rmtree(dest)
                except OSError as e:
                    _log.warning("删除旧版本失败（继续覆盖）: %s", e)

        try:
            shutil.copytree(workshop_dir, dest)
        except OSError as e:
            _log.error("复制下载内容失败: %s", e)
            _rollback_failed_install(config, mod_id, dest, stashed, backup_path)
            return _fail(f"复制下载内容失败: {e}")

        ok, reason = _verify_install(dest, mod_id)
        if not ok:
            _log.error("安装校验失败: %s — %s", mod_id, reason)
            _rollback_failed_install(config, mod_id, dest, stashed, backup_path)
            return _fail(reason)

        # Verification passed — only now record the install time in the metadata
        # DB (best effort: a DB hiccup must never fail an otherwise-good install).
        try:
            from rwmod.database import set_last_updated

            set_last_updated(folder_name, int(time.time()), instance_id=_instance_scope(config))
        except Exception:  # noqa: BLE001 — timestamp is auxiliary data
            _log.debug("写入安装时间戳失败: %s", folder_name)

        _discard_stash(stashed)
        if backup_path is not None:
            _cleanup_update_backups(config)
        return True


def download_batch(
    config: Config,
    mod_ids: list[str],
    force: bool = False,
    extra_out: list[str] | None = None,
    progress_cb: Callable[[str, float, float, float], None] | None = None,
    outcomes: dict[str, DownloadOutcome] | None = None,
) -> BatchResult:
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

    Returns a ``BatchResult`` — a ``dict[str, bool]`` (the historical
    contract) carrying ``.outcomes`` with one ``DownloadOutcome`` per id.
    ``outcomes``, when given, is filled with the same mapping for callers
    that prefer an out-parameter. ``extra_out`` collects ids that still need
    downloading (collection children / new dependencies).
    """
    out: dict[str, bool] = {mid: True for mid in dict.fromkeys(mod_ids)}
    outcome_map: dict[str, DownloadOutcome] = {}

    def _record(mid: str, ok: bool, kind: str = "", detail: str = "") -> None:
        outcome_map[mid] = DownloadOutcome(ok=ok, error_kind=kind, error_detail=detail)

    def _finish() -> BatchResult:
        if outcomes is not None:
            outcomes.clear()
            outcomes.update(outcome_map)
        return BatchResult(dict(out), dict(outcome_map))

    unique_ids = list(dict.fromkeys(mod_ids))
    # DB metadata is instance-scoped: everything below must look at the
    # installation this batch belongs to, not the process-global active one.
    iid = _instance_scope(config)
    if not force:
        existing = _find_existing_many(config.mods_dir, unique_ids, instance_id=iid)
        todo = [mid for mid in unique_ids if existing.get(mid) is None]
        for mid in unique_ids:
            if existing.get(mid) is not None:
                _record(mid, True, ErrorKind.OK, "已安装")
    else:
        todo = unique_ids
    if not todo:
        return _finish()

    # ── collections: probe up front; a collection id must NEVER reach SteamCMD ──
    # SteamCMD cannot download a collection: it "succeeds" with a folder that
    # has no About.xml, installation fails, and the Skymods fallback returns
    # 403. Probe every id first and remove collections from the batch no matter
    # how many children are already installed (the old filter lived inside
    # ``if children:`` and leaked the collection id when every child was
    # installed). A probe that cannot reach Steam fails the id explicitly
    # instead of guessing "not a collection".
    from rwmod.workshop import fetch_collection_children

    def _probe(mid: str) -> tuple[bool, list[str], str, str]:
        """Return (is_collection, children, error_kind, error_detail)."""
        try:
            is_coll, probe_failed = _classify_collection(mid)
        except Exception as e:  # noqa: BLE001 — a broken probe must fail closed
            return False, [], KIND_COLLECTION_PROBE_FAILED, f"合集探测异常: {e}"
        if probe_failed:
            return (
                False,
                [],
                KIND_COLLECTION_PROBE_FAILED,
                "无法确认是否为合集（Steam API 不可用）",
            )
        if not is_coll:
            return False, [], "", ""
        try:
            kids = fetch_collection_children(mid)
        except Exception as e:  # noqa: BLE001 — fail closed on fetch errors too
            return True, [], KIND_COLLECTION_EMPTY, f"无法获取合集子项: {e}"
        if not kids:
            return True, [], KIND_COLLECTION_EMPTY, "合集为空或无法获取子项"
        return True, list(kids), "", ""

    try:
        with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as ex:
            probes = list(ex.map(_probe, todo))
    except Exception as e:  # noqa: BLE001 — executor failure must fail closed too
        probes = [(False, [], KIND_COLLECTION_PROBE_FAILED, f"合集探测失败: {e}") for _ in todo]

    children: list[str] = []
    download_todo: list[str] = []
    for mid, (is_coll, kids, err_kind, err) in zip(todo, probes, strict=True):
        if err:
            # Unknown / empty collection: explicit failure, never a SteamCMD
            # fallback (that is how a collection used to be downloaded as a mod).
            out[mid] = False
            _record(mid, False, err_kind, err)
            _log.error("❌ %s：%s（已停止，未交给 SteamCMD）", mid, err)
            continue
        if not is_coll:
            download_todo.append(mid)
            continue
        # A big collection is mostly already installed. Handing every child
        # back unconditionally made the caller re-queue hundreds of installed
        # mods — one "download event" each — on every run.
        unique_kids = [k for k in dict.fromkeys(kids) if k not in out]
        fresh = unique_kids
        if not force:
            found = _find_existing_many(config.mods_dir, unique_kids, instance_id=iid)
            fresh = [k for k in unique_kids if found.get(k) is None]
        _log.info(
            "合集 ID %s：%s 个子项（%s 个已安装，跳过）交给后续下载",
            mid,
            len(unique_kids),
            len(unique_kids) - len(fresh),
        )
        children.extend(fresh)
        out[mid] = True  # collection itself is not a mod
        _record(mid, True, ErrorKind.OK, "")
    if children:
        if extra_out is not None:
            extra_out.extend(children)
        else:
            # No collector (direct call): keep the old recursive behavior.
            for cid in children:
                download_one(config, cid, force)
    todo = download_todo
    if not todo:
        return _finish()

    # ── batch SteamCMD download (one process, one login) ──────────
    steamcmd = SteamCMD(config.steamcmd_path)
    # Cancellation is scoped per installation: the batch registers under
    # (iid, mod id) so cancelling another instance's copy of the same workshop
    # id cannot kill this process.
    results = steamcmd.workshop_download_many(todo, progress_cb=progress_cb, instance_id=iid)
    # Defensive: every requested id must have a result entry.
    for mid in todo:
        if mid not in results:
            _log.error("批量下载缺少 %s 的结果（进程异常退出）", mid)
            out[mid] = False

    # ── install downloaded content in parallel ───────────────────
    ok_ids = [mid for mid, r in results.items() if r.success]
    install_detail: dict[str, str] = {}
    skymods_ok: dict[str, bool] = {}

    def _install(mid: str) -> bool:
        detail: list[str] = []
        try:
            # _install_content serializes on the destination folder, not the
            # workshop id — duplicate packageIds from different ids share it.
            ok = _install_content(config, mid, force, detail_out=detail)
        except Exception as e:  # noqa: BLE001 — one bad mod must not sink the batch
            _log.exception("安装 %s 时发生未预期异常", mid)
            ok = False
            detail.append(f"安装异常: {e}")
        if not ok:
            install_detail[mid] = detail[0] if detail else "安装失败"
        return ok

    installed: dict[str, bool] = {}
    if ok_ids:
        with ThreadPoolExecutor(max_workers=min(_BATCH_WORKERS, max(1, len(ok_ids)))) as ex:
            installed = dict(zip(ok_ids, ex.map(_install, ok_ids), strict=True))
        for mid in ok_ids:
            if installed[mid]:
                _collect_deps(config, mid, extra_out)
            else:
                if bool(getattr(config, "enable_skymods", False)):
                    _log.warning("安装失败（内容已下载），尝试 Skymods: %s", mid)
                # The helper owns the opt-in decision (it returns False when
                # Skymods is disabled), so do not duplicate that gate here:
                # doing so made the recovery path untestable and hid whether a
                # fallback was even considered.
                skymods_ok[mid] = _skymods_fallback(config, mid)
                out[mid] = skymods_ok.get(mid, False)

    # ── failures: retry individually (retry loop + Skymods) ──────
    env_logged = False
    for mid, r in results.items():
        if r.success:
            # DL-9: do not overwrite a Skymods recovery recorded above with
            # the failed local-install result — the mod is installed now.
            out[mid] = installed.get(mid, True) or skymods_ok.get(mid, False)
            if out[mid]:
                _record(mid, True, ErrorKind.OK, "")
            else:
                detail = install_detail.get(mid, "安装失败")
                if bool(getattr(config, "enable_skymods", False)) and not skymods_ok.get(
                    mid, False
                ):
                    detail = f"{detail}；Skymods 备用源也失败"
                _record(mid, False, KIND_INSTALL_FAILED, detail)
            continue
        if r.error_kind == ErrorKind.CANCELLED:
            # The batch was killed by a user cancel. Never retry or re-download
            # here — the queue re-queues unfinished siblings and keeps the
            # cancelled target terminal.
            out[mid] = False
            _record(
                mid,
                False,
                ErrorKind.CANCELLED,
                r.error_detail or "SteamCMD 批次被用户取消",
            )
            continue
        if r.error_kind == ErrorKind.ENVIRONMENT:
            # One environment failure affects the whole batch; logging it per
            # mod produced a wall of identical errors.
            if not env_logged:
                _log.error(
                    "❌ SteamCMD 环境错误：%s",
                    r.error_detail or ErrorKind.explain(r.error_kind),
                )
                env_logged = True
            out[mid] = False
            _record(mid, False, r.error_kind, r.error_detail or ErrorKind.explain(r.error_kind))
            continue
        if r.error_kind in ErrorKind.NON_RETRYABLE:
            _log.error(
                "❌ %s (%s): %s — 已跳过 Skymods（错误不可恢复）",
                mid,
                r.error_kind,
                r.error_detail or ErrorKind.explain(r.error_kind),
            )
            out[mid] = False
            _record(mid, False, r.error_kind, r.error_detail or ErrorKind.explain(r.error_kind))
            continue
        # Recoverable failure → per-mod retry path (own retries + Skymods).
        one_out: list[DownloadOutcome] = []
        out[mid] = download_one(config, mid, force, outcome_out=one_out)
        if one_out:
            outcome_map[mid] = one_out[0]
        else:
            _record(mid, out[mid])

    # Defensive: every requested id must carry an outcome.
    for mid, ok in out.items():
        outcome_map.setdefault(
            mid,
            DownloadOutcome(bool(ok), ErrorKind.OK if ok else ErrorKind.UNKNOWN, ""),
        )
    return _finish()


def download_one(
    config: Config,
    mod_id: str,
    force: bool = False,
    _visited: set[str] | None = None,
    outcome_out: list[DownloadOutcome] | None = None,
) -> bool:
    """Download a single mod. Returns True on success.

    Serializes concurrent downloads of the same mod via a per-mod lock,
    making the check-then-install sequence (find existing copy) atomic.

    Collection children and dependencies are collected while the lock is
    held but downloaded AFTER it is released — holding a per-mod lock across
    recursive downloads of *other* mods can deadlock two threads that lock
    in opposite order (A locks A, wants B; B locks B, wants A).

    ``_visited`` tracks ids already expanded in this download tree: a cyclic
    collection/dependency graph would otherwise recurse forever (A → B → A).
    ``outcome_out`` receives one ``DownloadOutcome`` for the *top-level* id
    (recursive child downloads do not append to it).
    """
    visited = set() if _visited is None else _visited
    if mod_id in visited:
        _log.debug("跳过循环子项: %s", mod_id)
        return False
    visited.add(mod_id)

    deferred: list[str] = []
    with _lock_for(mod_id):
        ok = _download_one_unlocked(config, mod_id, force, deferred, visited, outcome_out)
    for pid in deferred:
        _log.info("下载子项/依赖（锁外）: %s", pid)
        download_one(config, pid, force, _visited=visited)
    return ok


def _skymods_fallback(config: Config, mod_id: str) -> bool:
    """Try Skymods as fallback source (opt-in, circuit-broken)."""
    if not bool(getattr(config, "enable_skymods", False)):
        _log.debug("Skymods 备用源已禁用，跳过: %s", mod_id)
        return False
    from rwmod.skymods import skymods_blocked, try_skymods

    if skymods_blocked():
        _log.debug("Skymods 备用源熔断中，跳过: %s", mod_id)
        return False
    _log.info("尝试 Skymods 备用源...")
    result = try_skymods(mod_id, config)
    if result:
        _log.info("Skymods 备用源下载成功: %s → %s", mod_id, result)
        return True
    _log.error("Skymods 备用源也失败: %s", mod_id)
    return False
