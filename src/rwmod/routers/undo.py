"""Undo router — list & roll back destructive ModsConfig.xml operations."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.undo import list_undo_snapshots, undo_last

router = APIRouter(prefix="/api/undo", tags=["undo"])


@router.get("")
def api_list_undo(
    cfg: Config = Depends(get_config),
):
    """List available undo snapshots (newest first)."""
    from rwmod.profile import resolve_modsconfig_path

    path = resolve_modsconfig_path(cfg.rimworld_dir) or (cfg.rimworld_dir / "ModsConfig.xml")
    snapshots = [
        {"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)}
        for p in list_undo_snapshots()
    ]
    return {"snapshots": snapshots, "modsconfig_path": str(path)}


@router.post("")
def api_undo(cfg: Config = Depends(get_config)):
    """Restore the most recent pre-operation snapshot of ModsConfig.xml."""
    from rwmod.profile import resolve_modsconfig_path

    path = resolve_modsconfig_path(cfg.rimworld_dir) or (cfg.rimworld_dir / "ModsConfig.xml")
    return undo_last(path)
