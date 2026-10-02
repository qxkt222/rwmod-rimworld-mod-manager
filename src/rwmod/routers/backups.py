"""Backups router."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.models.schemas import BackupCleanupRequest, BackupRestoreRequest

router = APIRouter(prefix="/api", tags=["backups"])


@router.get("/backups")
def list_backups(
    workshop_id: str = "",
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import list_backups as _list

    return {
        "backups": _list(cfg.backup_dir, workshop_id=workshop_id if workshop_id.strip() else None),
        "backup_dir": str(cfg.backup_dir),
    }


@router.post("/backups/{workshop_id}/restore")
def restore_backup(
    workshop_id: str,
    payload: BackupRestoreRequest | None = None,
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import restore_mod

    filename = payload.filename if payload else None
    return restore_mod(
        cfg.mods_dir,
        workshop_id,
        cfg.backup_dir,
        backup_filename=filename or None,
    )


@router.delete("/backups/{filename}")
def delete_backup(
    filename: str,
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import delete_backup

    return {"ok": delete_backup(cfg.backup_dir, filename)}


@router.post("/backups/cleanup")
def cleanup_backups(
    payload: BackupCleanupRequest | None = None,
    cfg: Config = Depends(get_config),
):
    from rwmod.backup import cleanup_backups

    keep: int = payload.keep if payload else 5
    return {"ok": True, "deleted": cleanup_backups(cfg.backup_dir, keep_per_mod=keep)}
