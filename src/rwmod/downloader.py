"""Download + retry + copy-to-mods logic — with smart error handling."""

from __future__ import annotations

import logging
import random
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rwmod.config import Config
from rwmod.database import find_local_mods_by_workshop_id, find_local_mods_by_workshop_ids
from rwmod.steamcmd import DownloadResult, ErrorKind, SteamCMD
from rwmod.utils import safe_filename
from rwmod.xmlutil import parse_about_field

__all__ = [
    "download_one",
    "download_batch",
    "DownloadOutcome",
    "BatchResult",
    "explain_outcome",
    "_find_existing",
    "_pick_folder_name",
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

# Kinds produced by the downloader itself (not by SteamCMD's workshop log).
# They live here rather than in steamcmd.ErrorKind because they describe our
# pipeline, not a SteamCMD result line.
KIND_INSTALL_FAILED = "install_failed"
KIND_COLLECTION_PROBE_FAILED = "collection_probe_failed"
KIND_COLLECTION_EMPTY = "collection_empty"

_OUTCOME_HUMAN: dict[str, str] = {
    KIND_INSTALL_FAILED: "下载完成但安装失败",
    KIND_COLLECTION_PROBE_FAILED: "无法确认是否为合集（Steam API 不可用）",
    KIND_COLLECTION_EMPTY: "合集为空或无法获取子项",
}


@dataclass(frozen=True)
class DownloadOutcome:
    """Structured result of one download, safe to surface in the queue/UI.

    ``error_detail`` is for hover/tooltips and logs — keep it out of compact
    UI rows. ``error_kind`` is a stable machine code (``ErrorKind.*`` or one
    of the ``KIND_*`` constants above).
    """

    ok: bool
    error_kind: str = ""
    error_detail: str = ""


class BatchResult(dict[str, bool]):
    """``{mod_id: ok}`` plus the per-id structured outcomes.

    A dict subclass so every existing caller that expects
    ``dict[str, bool]`` keeps working; ``.outcomes`` carries the reasons.
    Queue/router callers read ``.outcomes`` instead of taking a new keyword,
    which also keeps older test doubles (plain dict returns) valid.
    """

    outcomes: dict[str, DownloadOutcome]

    def __init__(
        self,
        ok: dict[str, bool],
        outcomes: dict[str, DownloadOutcome],
    ) -> None:
        super().__init__(ok)
        self.outcomes = outcomes


def explain_outcome(kind: str) -> str:
    """Human-readable, short reason for a download outcome kind."""
    return _OUTCOME_HUMAN.get(kind) or ErrorKind.explain(kind)


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


def _verify_install(dest: Path, mod_id: str) -> tuple[bool, str]:
    """Check a freshly installed mod folder before declaring success.

    P1-2: a copy that exists is not necessarily the item we asked for — a
    truncated download, a wrong item, or an empty About.xml all looked like
    success before. Returns ``(ok, reason)``.

    ``PublishedFileId.txt`` is checked when present. Workshop content that
    genuinely has no such file (older/manual uploads) is accepted rather than
    rolled back; a *mismatch* is always a failure.
    """
    about = dest / "About" / "About.xml"
    if not about.is_file():
        return False, "安装后缺少 About/About.xml"
    try:
        if about.stat().st_size == 0:
            return False, "安装后的 About.xml 为空"
    except OSError as e:
        return False, f"无法读取 About.xml: {e}"

    published = dest / "About" / "PublishedFileId.txt"
    if published.is_file():
        try:
            value = published.read_text(encoding="utf-8").strip()
        except OSError as e:
            return False, f"无法读取 PublishedFileId.txt: {e}"
        if value != mod_id:
            return False, f"PublishedFileId 不一致（{value} != {mod_id}）"

    try:
        if not any(p.is_file() for p in dest.rglob("*")):
            return False, "安装内容为空"
    except OSError as e:
        return False, f"无法读取安装内容: {e}"
    return True, ""


def _persistent_backup(config: Config, mod_id: str, mod_dir: Path) -> Path | None:
    """Zip the installed copy before a force overwrite (P1-2).

    Unlike the stash (same-volume rename, lost if the process dies), this zip
    survives the update and can be restored later. ``update_backup=False``
    disables it. Failure never blocks the update.
    """
    if not bool(getattr(config, "update_backup", True)):
        return None
    try:
        from rwmod.backup import backup_mod

        return backup_mod(config.mods_dir, mod_id, mod_dir.name, config.backup_dir)
    except Exception as e:  # noqa: BLE001 — backup is best-effort
        _log.warning("更新前持久备份失败（继续更新）: %s", e)
        return None


def _cleanup_update_backups(config: Config) -> None:
    """Keep at most ``backup_keep`` zips per mod (best effort)."""
    try:
        from rwmod.backup import cleanup_backups

        keep = max(1, int(getattr(config, "backup_keep", 3)))
        cleanup_backups(config.backup_dir, keep_per_mod=keep)
    except Exception as e:  # noqa: BLE001 — cleanup must never fail the update
        _log.debug("清理旧备份失败: %s", e)


def _rollback_failed_install(
    config: Config,
    mod_id: str,
    dest: Path,
    stashed: Path | None,
    backup_path: Path | None,
) -> bool:
    """Restore the old copy after a failed install. Order: stash → backup zip.

    The failure may have left a partial/invalid copy at ``dest``; it must be
    removed first or the stashed copy cannot be renamed back into place.
    """
    if dest.exists():
        try:
            shutil.rmtree(dest)
        except OSError as e:
            _log.warning("回滚前删除失败副本失败: %s", e)

    if stashed is not None and stashed.exists():
        try:
            stashed.rename(dest)
            _log.info("下载失败，已从暂存目录回滚: %s", dest.name)
            return True
        except OSError as e:
            _log.warning("从暂存目录回滚失败: %s", e)

    if backup_path is not None:
        try:
            from rwmod.backup import restore_mod

            result = restore_mod(
                config.mods_dir,
                mod_id,
                config.backup_dir,
                backup_filename=backup_path.name,
            )
            if result.get("ok"):
                _log.info("下载失败，已从更新前备份回滚: %s", backup_path.name)
                return True
            _log.warning("从更新前备份回滚失败: %s", result.get("msg"))
        except Exception as e:  # noqa: BLE001 — rollback must never mask the error
            _log.warning("从更新前备份回滚异常: %s", e)

    # Last resort: newest backup for this mod. Only when this call actually
    # had an old copy to restore (a caller that owns the stash, e.g. the
    # single-mod path, restores it itself after this returns).
    if stashed is not None or backup_path is not None:
        _auto_restore_backup(config, mod_id)
    return False


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
            # Could not rename the old copy aside (locked files?) — fall back
            # to the previous delete-then-copy behaviour.
            shutil.rmtree(dest)

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
    results = steamcmd.workshop_download_many(todo, progress_cb=progress_cb)
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
        with _lock_for(mid):
            ok = _install_content(config, mid, force, detail_out=detail)
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


def _download_one_unlocked(
    config: Config,
    mod_id: str,
    force: bool = False,
    deferred: list[str] | None = None,
    visited: set[str] | None = None,
    outcome_out: list[DownloadOutcome] | None = None,
) -> bool:
    """Core download logic. Caller must already hold the per-mod lock.

    ``deferred`` (when given) receives IDs of collection children /
    dependencies that still need downloading; the caller processes them
    after releasing the lock. ``outcome_out`` receives the structured result.
    """
    steamcmd = SteamCMD(config.steamcmd_path)

    def _record(ok: bool, kind: str = "", detail: str = "") -> None:
        if outcome_out is not None:
            outcome_out.append(DownloadOutcome(ok=ok, error_kind=kind, error_detail=detail))

    # Force-overwrite: the existing mod is renamed aside (not deleted) so a
    # failed download can roll it back. Deleting first (the old rmtree) left
    # a window where a failed download with no backup permanently lost the mod.
    old_backed_up = False
    iid = _instance_scope(config)
    existing = _find_existing(config.mods_dir, mod_id, instance_id=iid)
    stashed: Path | None = None
    backup_path: Path | None = None
    if existing:
        if force:
            # Persistent zip first (survives a crash), then the same-volume
            # stash for a fast rollback.
            backup_path = _persistent_backup(config, mod_id, existing)
            stashed = _stash_for_overwrite(existing)
            if stashed is not None:
                old_backed_up = True
            else:
                _log.warning("暂存旧版失败（将以备份方式覆盖）")
                try:
                    from rwmod.backup import backup_mod

                    if backup_mod(config.mods_dir, mod_id, existing.name, config.backup_dir):
                        old_backed_up = True
                except Exception as e2:  # noqa: BLE001
                    _log.warning("备份失败: %s", e2)
                shutil.rmtree(existing)
        else:
            _log.info("已安装: %s", existing.name)
            _record(True, ErrorKind.OK, "已安装")
            return True

    def _restore_old_copy() -> None:
        """Undo a force-overwrite stash after an aborted (non-SteamCMD) path.

        The stash/backup above runs *before* collection detection, so an early
        return from the collection block must put the old copy back — otherwise
        a failed probe during a force update would leave the mod renamed aside
        and the game without it.
        """
        if not old_backed_up:
            return
        if not _rollback_stash(config, mod_id, stashed):
            _auto_restore_backup(config, mod_id)

    # ── collection detection (Web API, no SteamCMD needed) ──────────
    # SteamCMD cannot download collections — only individual mod files.
    # If the probe itself fails we stop instead of falling through to SteamCMD:
    # that fallback is exactly how a collection id ends up "downloaded" as a
    # single mod (no About.xml → Skymods 403).
    try:
        from rwmod.workshop import fetch_collection_children

        is_coll, probe_failed = _classify_collection(mod_id)
        if probe_failed:
            _log.error("无法确认 %s 是否为合集（Steam API 不可用），已停止下载", mod_id)
            _restore_old_copy()
            _record(False, KIND_COLLECTION_PROBE_FAILED, "无法确认是否为合集（Steam API 不可用）")
            return False
        if is_coll:
            _log.info("检测到合集 ID: %s，获取子项...", mod_id)
            children = fetch_collection_children(mod_id)
            if not children:
                _log.warning("合集 %s 为空或无法获取子项", mod_id)
                _restore_old_copy()
                _record(False, KIND_COLLECTION_EMPTY, "合集为空或无法获取子项")
                return False
            _log.info("合集包含 %s 个 Mod，交给锁外队列下载...", len(children))
            if deferred is None:
                # 无收集器（例如直接调用）时保持原行为：逐个递归下载
                ok = 0
                for i, cid in enumerate(children, 1):
                    _log.info("[%s/%s] 合集子项 %s", i, len(children), cid)
                    if download_one(config, cid, force=force, _visited=visited):
                        ok += 1
                if ok:
                    _record(True, ErrorKind.OK, "")
                    return True
                _restore_old_copy()
                _record(False, ErrorKind.UNKNOWN, "合集子项全部下载失败")
                return False
            for cid in children:
                if force or not _find_existing(config.mods_dir, cid, instance_id=iid):
                    deferred.append(cid)
            # DL-6: "nothing left to download" is success, not failure. Every
            # child already installed means the collection is satisfied; the
            # caller (queue/router) must report done, not failed.
            ok = True
            _record(ok, ErrorKind.OK, "无待下载子项" if not deferred else "")
            return ok
    except Exception as e:
        # Fail closed: an ambiguous probe must never become a SteamCMD download.
        _log.error("合集探测失败，已停止下载 %s（不交给 SteamCMD）: %s", mod_id, e)
        _restore_old_copy()
        _record(False, KIND_COLLECTION_PROBE_FAILED, f"合集探测失败: {e}")
        return False

    last_result: DownloadResult | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        _log.info("尝试 %s/%s...", attempt, MAX_RETRIES + 1)
        result = steamcmd.workshop_download(mod_id)
        last_result = result

        # Log relevant output lines
        for line in result.output_lines:
            low = line.lower()
            if any(kw in low for kw in ("download", "success", "error", "fail", "item")):
                _log.info("  %s", line)

        if result.success:
            break

        # The batch was killed by a user cancel — retrying would immediately
        # undo the cancellation.
        if result.error_kind == ErrorKind.CANCELLED:
            _log.info("批次被取消，停止重试: %s", mod_id)
            break

        # Permanent errors → no point retrying
        if result.error_kind in ErrorKind.NON_RETRYABLE:
            _log.warning(
                "不可重试错误 [%s]: %s",
                result.error_kind,
                result.error_detail or ErrorKind.explain(result.error_kind),
            )
            break

        # Transient errors → retry with exponential backoff (5 → 15 → 45s).
        if attempt <= MAX_RETRIES:
            delay = _retry_delay(attempt)
            _log.warning("SteamCMD 错误 [%s]，%.1fs 后重试...", result.error_kind, delay)
            time.sleep(delay)

    # ── handle successful download ───────────────────────────────
    if last_result and last_result.success:
        install_detail: list[str] = []
        if not _install_content(config, mod_id, force, detail_out=install_detail):
            reason = install_detail[0] if install_detail else "安装失败"
            _log.warning("安装下载内容失败，尝试 Skymods: %s", mod_id)
            if _skymods_fallback(config, mod_id):
                _discard_stash(stashed)
                if backup_path is not None:
                    _cleanup_update_backups(config)
                _record(True, ErrorKind.OK, "")
                return True
            if old_backed_up and existing is not None:
                _rollback_failed_install(config, mod_id, existing, stashed, backup_path)
            detail = reason
            if bool(getattr(config, "enable_skymods", False)):
                detail = f"{detail}；Skymods 备用源也失败"
            _record(False, KIND_INSTALL_FAILED, detail)
            return False
        _log.info("下载完成: %s", mod_id)
        # Force-overwrite succeeded — the stashed old copy is now obsolete.
        _discard_stash(stashed)
        if backup_path is not None:
            _cleanup_update_backups(config)

        # Auto-download dependencies (collected, downloaded outside the lock)
        _collect_deps(config, mod_id, deferred)
        _record(True, ErrorKind.OK, "")
        return True

    # ── handle failure ───────────────────────────────────────────
    if last_result:
        err_msg = last_result.error_detail or ErrorKind.explain(last_result.error_kind)
        _log.error("下载失败: %s — %s", mod_id, err_msg)

    # Only try Skymods if the error is potentially recoverable and the source
    # is enabled (it is opt-in; smods.ru answers plain urllib with a 403).
    skymods_attempted = False
    if (
        last_result
        and last_result.error_kind not in ErrorKind.NON_RETRYABLE
        and last_result.error_kind != ErrorKind.CANCELLED
        and bool(getattr(config, "enable_skymods", False))
    ):
        skymods_attempted = True
        if _skymods_fallback(config, mod_id):
            # Skymods installed a copy — the force-overwrite stash is obsolete.
            _discard_stash(stashed)
            _record(True, ErrorKind.OK, "")
            return True

    # For non-retryable errors, explain clearly and skip Skymods; a user
    # cancel is not an error worth a ❌ line.
    if last_result and last_result.error_kind == ErrorKind.CANCELLED:
        _log.info("下载已取消: %s", mod_id)
    elif last_result:
        _log.error(
            "❌ %s (%s): %s — 已跳过 Skymods（错误不可恢复）",
            mod_id,
            last_result.error_kind,
            err_msg,
        )
    else:
        _log.error("❌ %s: 下载失败", mod_id)

    # force 覆盖时旧版已被移开——下载失败则按 stash → 更新前备份的顺序回滚。
    if old_backed_up and existing is not None:
        _rollback_failed_install(config, mod_id, existing, stashed, backup_path)
    if last_result:
        detail = err_msg
        if skymods_attempted:
            detail = f"{detail}；Skymods 备用源也失败"
        _record(False, last_result.error_kind, detail)
    else:
        _record(False, ErrorKind.UNKNOWN, "下载失败")
    return False


def _stash_for_overwrite(dest: Path) -> Path | None:
    """Rename *dest* aside before a force-overwrite.

    Returns the stash path (``.rwmod_stash_<name>_<timestamp>`` next to the
    original), or None when the rename itself failed — the caller then falls
    back to the old delete-then-copy behaviour.
    """
    stashed = dest.parent / (
        f".rwmod_stash_{safe_filename(dest.name, allow_empty=False)}_{int(time.time())}"
    )
    try:
        dest.rename(stashed)
    except OSError as e:
        _log.warning("暂存旧版失败: %s", e)
        return None
    _log.info("覆盖前暂存旧版: %s → %s", dest.name, stashed.name)
    return stashed


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
                    if _find_existing(config.mods_dir, dep_id, instance_id=_instance_scope(config)):
                        queued.add(dep_id)
                        continue
                    _log.info("收集依赖: %s", dep_id)
                    if deferred is not None:
                        deferred.append(dep_id)
                    queued.add(dep_id)
                    queue.append(dep_id)
    except Exception as e:
        _log.warning("依赖检测失败: %s", e)
