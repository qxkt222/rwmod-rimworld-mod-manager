"""Backups router.

Failure contract: a missing backup or a failed restore raises an ``errors.py``
exception, so the status code carries the failure. Previously ``restore_mod``
answered 200 with ``{ok: false, msg}`` and the client had to inspect the body to
learn that nothing happened.
"""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.errors import ModNotFoundError, RwmodError, ValidationError
from rwmod.models.schemas import (
    BackupCleanupRequest,
    BackupCleanupResponse,
    BackupDeleteResponse,
    BackupListResponse,
    BackupRestoreRequest,
    BackupRestoreResponse,
)

router = APIRouter(prefix="/api", tags=["backups"])


@router.get("/backups", response_model=BackupListResponse)
def list_backups(
    workshop_id: str = "",
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import list_backups as _list

    return {
        "backups": _list(cfg.backup_dir, workshop_id=workshop_id if workshop_id.strip() else None),
        "backup_dir": str(cfg.backup_dir),
    }


@router.post("/backups/{workshop_id}/restore", response_model=BackupRestoreResponse)
def restore_backup(
    workshop_id: str,
    payload: BackupRestoreRequest | None = None,
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import restore_mod

    filename = payload.filename if payload else None
    result = restore_mod(
        cfg.mods_dir,
        workshop_id,
        cfg.backup_dir,
        backup_filename=filename or None,
    )
    if not result.get("ok"):
        raise _restore_error(str(result.get("msg") or "恢复失败"))
    return result


def _restore_error(msg: str) -> Exception:
    """Map ``restore_mod``'s message onto the right status code.

    The library reports every failure as ``{ok: False, msg}``; the router is
    where that becomes a status code. "not found" is 404, a bad request/archive
    is 400, and a genuine write failure stays 500.
    """
    if "未找到" in msg or "不存在" in msg:
        return ModNotFoundError(msg)
    if "无效" in msg or "不完整" in msg or "解压失败" in msg:
        return ValidationError(msg)
    return RwmodError(msg)


@router.delete("/backups/{filename}", response_model=BackupDeleteResponse)
def delete_backup(
    filename: str,
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import delete_backup

    if not delete_backup(cfg.backup_dir, filename):
        # An invalid name and a missing file are both "nothing was deleted";
        # the caller asked to delete a backup that isn't there.
        raise ModNotFoundError(f"备份不存在或文件名非法: {filename}")
    return {"ok": True}


@router.post("/backups/cleanup", response_model=BackupCleanupResponse)
def cleanup_backups(
    payload: BackupCleanupRequest | None = None,
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import cleanup_backups

    keep: int = payload.keep if payload else 5
    return {"ok": True, "deleted": cleanup_backups(cfg.backup_dir, keep_per_mod=keep)}
