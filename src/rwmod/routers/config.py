"""Config router."""

import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from rwmod.config import (
    Config,
    InstanceConfig,
    is_allowed_steamcmd_name,
    is_valid_instance_id,
)
from rwmod.deps import get_config
from rwmod.models.schemas import ConfigResponse, ConfigUpdateRequest, InstanceCreateRequest

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


# Host names that address the local machine through a UNC path.
_LOCAL_UNC_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ".", "?"})


def _is_root(p: Path) -> bool:
    """True if p is a filesystem root (``C:\\`` or ``/``)."""
    if sys.platform.startswith("win"):
        return len(p.parts) == 1 and p.parts[0].endswith("\\")
    return p == Path("/")


def _windows_alias_candidates(raw: str) -> tuple[list[str], bool]:
    """Normalise Windows path aliases for the system-directory blocklist.

    ``Path.resolve()`` mangles the extended-length / device / UNC namespaces
    (``\\\\?\\C:\\Windows`` resolves to a drive-relative string), so the raw
    spelling is normalised too. Returns ``(candidate_norms, is_device)`` where
    ``is_device`` marks a namespace that can never be a legitimate directory
    (device paths and UNC admin shares such as ``\\\\localhost\\C$``).
    """
    s = raw.replace("/", "\\")
    low = s.lower()
    is_device = False
    if low.startswith("\\\\?\\unc\\"):
        # Extended-length UNC: \\?\UNC\host\share -> \\host\share
        s = "\\\\" + s[8:]
    elif low.startswith("\\\\?\\") or low.startswith("\\??\\"):
        s = s[4:]
    elif low.startswith("\\\\.\\"):
        # \\.\PhysicalDrive0, \\.\C: — device namespace, never a directory.
        is_device = True

    candidates = [os.path.normcase(s)]
    if s.startswith("\\\\"):
        parts = s[2:].split("\\", 2)
        host = parts[0].lower() if parts else ""
        share = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        if share.endswith("$"):
            # Admin / hidden share (C$, ADMIN$, ...) aliases a volume. Never
            # accept one as a mods/backup directory.
            is_device = True
        elif host in _LOCAL_UNC_HOSTS and re.fullmatch(r"[A-Za-z]\$?", share):
            # \\localhost\C\Windows is not valid UNC, but be defensive.
            candidates.append(os.path.normcase(f"{share.rstrip('$')}:\\{rest}"))
    return candidates, is_device


def _reject_unsafe_dir(p: Path, field: str, raw: str | None = None) -> None:
    """Reject filesystem roots, the working directory and system directories.

    ``raw`` is the user-supplied spelling (before ``resolve()``) so Windows
    namespace aliases can be compared against the same blocklist.
    """
    if _is_root(p):
        raise HTTPException(400, f"{field} 不能指向磁盘根目录: {str(p)!r}")
    if p == Path.cwd():
        raise HTTPException(400, f"{field} 不能指向程序运行目录: {str(p)!r}")
    if sys.platform.startswith("win"):
        candidates, is_device = _windows_alias_candidates(raw or str(p))
        candidates.append(os.path.normcase(str(p)))
        if is_device:
            raise HTTPException(400, f"{field} 不能指向设备/管理共享路径: {raw or str(p)!r}")
        for norm in candidates:
            if any(norm == d or norm.startswith(d + "\\") for d in _WINDOWS_PREFIX_BLOCK):
                raise HTTPException(400, f"{field} 不能指向系统目录: {str(p)!r}")
            if any(norm == d for d in _WINDOWS_EXACT_BLOCK):
                raise HTTPException(400, f"{field} 不能指向系统目录: {str(p)!r}")
            if re.fullmatch(r"[a-z]:\\?", norm):
                raise HTTPException(400, f"{field} 不能指向磁盘根目录: {str(p)!r}")


def _validate_dir_path(raw: str, field: str) -> Path:
    """Validate a user-supplied directory path before writing it to config."""
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(400, f"{field} 必须是绝对路径: {raw!r}")
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, f"{field} 路径无效: {raw!r}") from None
    _reject_unsafe_dir(resolved, field, raw)
    return p


def _validate_steamcmd_path(raw: str, cfg: Config | None = None) -> Path:
    """steamcmd_path must point at a real ``steamcmd`` executable.

    The old check only asked for "absolute path to a regular file" — but this
    path is *executed* by ``/api/steamcmd/check`` and by every download. Since
    ``/api/mods/import-local`` unpacks a user-supplied zip into ``mods_dir``,
    the old rule allowed upload-any-exe → point the setting at it → execute,
    i.e. arbitrary code execution from the UI. Restrict the basename and refuse
    any path inside a directory this app writes uploads into.
    """
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(400, f"steamcmd_path 必须是绝对路径: {raw!r}")
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, f"steamcmd_path 路径无效: {raw!r}") from None
    if resolved.is_dir():
        raise HTTPException(400, f"steamcmd_path 不能是目录: {raw!r}")
    if not is_allowed_steamcmd_name(resolved):
        raise HTTPException(
            400,
            "steamcmd_path 必须指向 steamcmd.exe（或 steamcmd.sh），"
            f"当前文件名不可执行: {resolved.name!r}",
        )
    _reject_unsafe_dir(resolved, "steamcmd_path", raw)
    if cfg is not None:
        _reject_inside_upload_dirs(resolved, cfg, raw)
    return p


def _reject_inside_upload_dirs(
    path: Path, cfg: Config, raw: str, extra_dirs: tuple[Path, ...] = ()
) -> None:
    """Refuse an executable living inside a directory the API can write to.

    ``mods_dir`` receives uploaded mod zips and ``backup_dir`` receives
    restored archives; treating those as an executable source would rebuild
    the upload→execute chain the basename check just closed. ``extra_dirs``
    lets a not-yet-registered instance's directories be checked too.
    """
    candidates = [cfg.mods_dir, cfg.backup_dir, *extra_dirs]
    for inst in cfg.instances:
        candidates.extend((inst.mods_dir, inst.backup_dir or inst.mods_dir / "_backups"))
    resolved = path.resolve()
    for directory in candidates:
        try:
            target = Path(directory).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == target or resolved.is_relative_to(target):
            raise HTTPException(
                400,
                f"steamcmd_path 不能位于可上传目录内: {raw!r}",
            )


def _instance_payload(instance: InstanceConfig) -> dict:
    return {
        "id": instance.id,
        "name": instance.name,
        "steamcmd_path": str(instance.steamcmd_path),
        "mods_dir": str(instance.mods_dir),
        "rimworld_dir": str(instance.rimworld_dir),
        "backup_dir": str(instance.backup_dir),
        "config_dir": str(instance.config_dir) if instance.config_dir is not None else None,
    }


@router.get("/config", response_model=ConfigResponse)
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
        "instances": [_instance_payload(i) for i in cfg.instances],
        "active_instance": cfg.active_instance,
    }


@router.post("/config/instances")
def create_instance(
    payload: InstanceCreateRequest,
    cfg: Config = Depends(get_config),
):
    """Register another RimWorld installation (does not activate it)."""
    mods_dir = _validate_dir_path(payload.mods_dir, "mods_dir")
    rimworld_raw = payload.rimworld_dir
    rimworld_dir = (
        _validate_dir_path(rimworld_raw, "rimworld_dir") if rimworld_raw else mods_dir.parent
    )
    backup_dir = (
        _validate_dir_path(payload.backup_dir, "backup_dir") if payload.backup_dir else None
    )
    config_dir = (
        _validate_dir_path(payload.config_dir, "config_dir") if payload.config_dir else None
    )
    steamcmd_path = None
    if payload.steamcmd_path:
        steamcmd_path = _validate_steamcmd_path(payload.steamcmd_path, cfg)
        # The new instance is not registered yet, so its own directories must
        # be passed explicitly to the upload-directory guard.
        _reject_inside_upload_dirs(
            steamcmd_path,
            cfg,
            payload.steamcmd_path,
            extra_dirs=(mods_dir, backup_dir or mods_dir / "_backups"),
        )

    instance_id = (payload.id or "").strip() or None
    if instance_id is not None and not is_valid_instance_id(instance_id):
        raise HTTPException(400, f"非法实例 id: {instance_id!r}")
    try:
        instance = cfg.add_instance(
            instance_id=instance_id,
            name=(payload.name or "").strip() or None,
            steamcmd_path=steamcmd_path,
            mods_dir=mods_dir,
            rimworld_dir=rimworld_dir,
            backup_dir=backup_dir,
            config_dir=config_dir,
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    cfg.save()
    return {"ok": True, "instance": _instance_payload(instance)}


@router.post("/config/instances/{instance_id}/activate")
def activate_instance(
    instance_id: str,
    cfg: Config = Depends(get_config),
):
    """Switch the active instance.

    Refused with 409 while downloads are pending/downloading: switching would
    point the running queue at another mods directory mid-flight.
    """
    if cfg.get_instance(instance_id) is None:
        raise HTTPException(404, f"实例不存在: {instance_id}")
    if cfg.active_instance != instance_id:
        pending = _pending_downloads()
        if pending:
            raise HTTPException(
                409,
                f"队列中还有 {len(pending)} 个未完成的下载任务，请等待完成或清空队列后再切换实例",
            )
        cfg.activate_instance(instance_id)
        cfg.save()
        # The in-memory metadata partition is keyed by mods_dir already, but a
        # switch is a hard boundary: drop it so nothing stale survives.
        from rwmod.mod_cache import clear_memory_cache

        clear_memory_cache()
    return {"ok": True, "active_instance": cfg.active_instance}


@router.delete("/config/instances/{instance_id}")
def delete_instance(
    instance_id: str,
    cfg: Config = Depends(get_config),
):
    if cfg.get_instance(instance_id) is None:
        raise HTTPException(404, f"实例不存在: {instance_id}")
    if instance_id == cfg.active_instance:
        raise HTTPException(400, "不能删除活动实例，请先切换到其他实例")
    if len(cfg.instances) <= 1:
        raise HTTPException(400, "至少保留一个实例")
    try:
        cfg.remove_instance(instance_id)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    cfg.save()
    return {"ok": True}


def _pending_downloads() -> list[dict]:
    """Pending/downloading queue rows; an unprepared DB counts as empty."""
    import sqlite3

    from rwmod.database import queue_load_pending

    try:
        return queue_load_pending()
    except sqlite3.Error:
        return []


@router.post("/config")
def update_config(
    payload: ConfigUpdateRequest,
    cfg: Config = Depends(get_config),
):
    # model_fields_set, not truthiness: "key omitted" must stay distinguishable
    # from "key sent as an empty string", which the old `in payload` check gave us.
    sent = payload.model_fields_set
    if "steamcmd_path" in sent:
        cfg.steamcmd_path = _validate_steamcmd_path(payload.steamcmd_path or "", cfg)
    if "mods_dir" in sent:
        cfg.mods_dir = _validate_dir_path(payload.mods_dir or "", "mods_dir")
    if "rimworld_dir" in sent:
        cfg.rimworld_dir = _validate_dir_path(payload.rimworld_dir or "", "rimworld_dir")
    if "backup_dir" in sent:
        cfg.backup_dir = _validate_dir_path(payload.backup_dir or "", "backup_dir")
    if "steam_api_key" in sent:
        cfg.steam_api_key = payload.steam_api_key or ""
    cfg.save()
    return {"ok": True}
