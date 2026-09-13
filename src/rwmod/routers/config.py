"""Config router."""

import os
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from rwmod.config import Config
from rwmod.deps import get_config

router = APIRouter(prefix="/api", tags=["config"])

# Directories that must never hold mods/backups — two classes:
#  - prefix-blocked: no path under them is a sane target (Windows dirs).
#  - exact-blocked: the dir itself is off-limits, but user subpaths under it
#    (e.g. C:\Users\me\RimWorld\Mods) are completely normal.
_WINDOWS_PREFIX_BLOCK: tuple[str, ...] = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "c:\\$recycle.bin",
)
_WINDOWS_EXACT_BLOCK: tuple[str, ...] = ("c:\\users",)


def _is_root(p: Path) -> bool:
    """True if p is a filesystem root (``C:\\`` or ``/``)."""
    if sys.platform.startswith("win"):
        return len(p.parts) == 1 and p.parts[0].endswith("\\")
    return p == Path("/")


def _reject_unsafe_dir(p: Path, field: str) -> None:
    """Reject filesystem roots, the working directory and system directories."""
    if _is_root(p):
        raise HTTPException(400, f"{field} 不能指向磁盘根目录: {str(p)!r}")
    if p == Path.cwd():
        raise HTTPException(400, f"{field} 不能指向程序运行目录: {str(p)!r}")
    if sys.platform.startswith("win"):
        norm = os.path.normcase(str(p))
        if any(norm == d or norm.startswith(d + "\\") for d in _WINDOWS_PREFIX_BLOCK):
            raise HTTPException(400, f"{field} 不能指向系统目录: {str(p)!r}")
        if any(norm == d for d in _WINDOWS_EXACT_BLOCK):
            raise HTTPException(400, f"{field} 不能指向系统目录: {str(p)!r}")


def _validate_dir_path(raw: str, field: str) -> Path:
    """Validate a user-supplied directory path before writing it to config."""
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(400, f"{field} 必须是绝对路径: {raw!r}")
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, f"{field} 路径无效: {raw!r}") from None
    _reject_unsafe_dir(resolved, field)
    return p


def _validate_steamcmd_path(raw: str) -> Path:
    """steamcmd_path must be an absolute path to a regular file."""
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(400, f"steamcmd_path 必须是绝对路径: {raw!r}")
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, f"steamcmd_path 路径无效: {raw!r}") from None
    if resolved.is_dir():
        raise HTTPException(400, f"steamcmd_path 不能是目录: {raw!r}")
    _reject_unsafe_dir(resolved, "steamcmd_path")
    return p


@router.get("/config")
def get_config_route(
    cfg: Config = Depends(get_config),
):
    return {
        "steamcmd_path": str(cfg.steamcmd_path),
        "mods_dir": str(cfg.mods_dir),
        "rimworld_dir": str(cfg.rimworld_dir),
        "backup_dir": str(cfg.backup_dir),
        # Never expose the raw API key to the client — only whether one is set.
        "has_steam_api_key": bool(cfg.steam_api_key),
        "steamcmd_exists": cfg.steamcmd_path.exists(),
        "mods_dir_exists": cfg.mods_dir.exists(),
    }


@router.post("/config")
def update_config(
    payload: dict,
    cfg: Config = Depends(get_config),
):
    if "steamcmd_path" in payload:
        cfg.steamcmd_path = _validate_steamcmd_path(str(payload["steamcmd_path"]))
    if "mods_dir" in payload:
        cfg.mods_dir = _validate_dir_path(str(payload["mods_dir"]), "mods_dir")
    if "rimworld_dir" in payload:
        cfg.rimworld_dir = _validate_dir_path(str(payload["rimworld_dir"]), "rimworld_dir")
    if "backup_dir" in payload:
        cfg.backup_dir = _validate_dir_path(str(payload["backup_dir"]), "backup_dir")
    if "steam_api_key" in payload:
        cfg.steam_api_key = str(payload["steam_api_key"])
    cfg.save()
    return {"ok": True}
