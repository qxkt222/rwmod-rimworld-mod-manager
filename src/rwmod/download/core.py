"""Single-mod download state machine: collection probe → retries → install.

This is the body of ``rwmod.downloader._download_one_unlocked``. It resolves
the handful of names that tests and callers patch at the historical
``rwmod.downloader`` path (``SteamCMD``, ``_skymods_fallback``,
``_install_content``, ``download_one``, ``_retry_delay``) through the module
object at call time, so moving the function out of ``downloader.py`` does not
break those patch points.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from rwmod.config import Config
from rwmod.download.deps import _collect_deps
from rwmod.download.install import (
    _auto_restore_backup,
    _cleanup_update_backups,
    _discard_stash,
    _persistent_backup,
    _rollback_failed_install,
    _rollback_stash,
    _stash_for_overwrite,
)
from rwmod.download.outcomes import (
    KIND_COLLECTION_EMPTY,
    KIND_COLLECTION_PROBE_FAILED,
    KIND_INSTALL_FAILED,
    DownloadOutcome,
)
from rwmod.steamcmd import DownloadResult, ErrorKind

_log = logging.getLogger(__name__)


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
    from rwmod import downloader as _dl

    steamcmd = _dl.SteamCMD(config.steamcmd_path)

    def _record(ok: bool, kind: str = "", detail: str = "") -> None:
        if outcome_out is not None:
            outcome_out.append(DownloadOutcome(ok=ok, error_kind=kind, error_detail=detail))

    # Force-overwrite: the existing mod is renamed aside (not deleted) so a
    # failed download can roll it back. Deleting first (the old rmtree) left
    # a window where a failed download with no backup permanently lost the mod.
    old_backed_up = False
    iid = _dl._instance_scope(config)
    existing = _dl._find_existing(config.mods_dir, mod_id, instance_id=iid)
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
            elif backup_path is not None:
                # The zip holds the old copy; a failed copy can restore it.
                old_backed_up = True
                _log.warning("暂存旧版失败，改用更新前备份回滚: %s", existing.name)
                try:
                    shutil.rmtree(existing)
                except OSError as e:
                    # The copy below will fail and roll back from the zip.
                    _log.warning("删除旧版本失败（继续覆盖）: %s", e)
            else:
                # No stash and no backup: deleting the only copy is exactly
                # the data-loss bug. Keep the old version and fail this mod.
                _log.error("无法暂存旧版且无可用备份，保留旧版并取消更新: %s", mod_id)
                _record(False, KIND_INSTALL_FAILED, "无法暂存旧版且无可用备份，已保留旧版")
                return False
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

        is_coll, probe_failed = _dl._classify_collection(mod_id)
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
                    if _dl.download_one(config, cid, force=force, _visited=visited):
                        ok += 1
                if ok:
                    _record(True, ErrorKind.OK, "")
                    return True
                _restore_old_copy()
                _record(False, ErrorKind.UNKNOWN, "合集子项全部下载失败")
                return False
            for cid in children:
                if force or not _dl._find_existing(config.mods_dir, cid, instance_id=iid):
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

    for attempt in range(1, _dl.MAX_RETRIES + 2):
        _log.info("尝试 %s/%s...", attempt, _dl.MAX_RETRIES + 1)
        result = steamcmd.workshop_download(mod_id, instance_id=iid)
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
        if attempt <= _dl.MAX_RETRIES:
            delay = _dl._retry_delay(attempt)
            _log.warning("SteamCMD 错误 [%s]，%.1fs 后重试...", result.error_kind, delay)
            time.sleep(delay)

    # ── handle successful download ───────────────────────────────
    if last_result and last_result.success:
        install_detail: list[str] = []
        if not _dl._install_content(config, mod_id, force, detail_out=install_detail):
            reason = install_detail[0] if install_detail else "安装失败"
            _log.warning("安装下载内容失败，尝试 Skymods: %s", mod_id)
            if _dl._skymods_fallback(config, mod_id):
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
        if _dl._skymods_fallback(config, mod_id):
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
