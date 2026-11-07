"""Undo router — list & roll back destructive ModsConfig.xml operations."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.errors import ModNotFoundError, RwmodError
from rwmod.undo import list_undo_snapshots, undo_last

router = APIRouter(prefix="/api/undo", tags=["undo"])


@router.get("")
def api_list_undo(
    cfg: Config = Depends(get_config),
):
    """List available undo snapshots (newest first)."""
    from rwmod.profile import modsconfig_target

    path = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    snapshots = [
        {"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)}
        for p in list_undo_snapshots()
    ]
    return {"snapshots": snapshots, "modsconfig_path": str(path)}


@router.post("")
def api_undo(cfg: Config = Depends(get_config)):
    """Restore the most recent pre-operation snapshot of ModsConfig.xml."""
    from rwmod.profile import modsconfig_target

    # cfg.config_dir (when set) is authoritative; the guard raises before the
    # snapshot is copied anywhere outside the test sandbox.
    path = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    result = undo_last(path)
    if not result.get("ok"):
        msg = str(result.get("msg") or "撤销失败")
        if "没有可撤销" in msg:
            raise ModNotFoundError(msg)
        raise RwmodError(msg)
    return result
