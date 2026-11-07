"""Install verification, stashing, persistent backups and rollback helpers.

Everything here is called by the install/copy step in ``rwmod.downloader``.
None of it launches SteamCMD; the helpers only touch the mods directory.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from rwmod.config import Config
from rwmod.utils import safe_filename
from rwmod.xmlutil import parse_about_field

_log = logging.getLogger(__name__)


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

    With neither a stash nor a backup there is nothing to restore: ``dest``
    then still holds the *untouched old version* (the install aborted before
    deleting it), so it must not be removed — that would turn a failed update
    into data loss.
    """
    has_restore = (stashed is not None and stashed.exists()) or backup_path is not None
    if dest.exists() and has_restore:
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


def _stash_for_overwrite(dest: Path) -> Path | None:
    """Rename *dest* aside before a force-overwrite.

    Returns the stash path (``.rwmod_stash_<name>_<timestamp>`` next to the
    original), or None when the rename itself failed — the caller must then
    fall back to a persistent backup (and must NOT delete the old copy when
    no backup exists).
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
