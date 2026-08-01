"""Dashboard router."""

import os
import time
from typing import Any

from fastapi import APIRouter, Depends

from rwmod.autoupdate import AutoUpdateManager
from rwmod.config import Config
from rwmod.database import get_download_history
from rwmod.deps import get_autoupdate, get_config
from rwmod.steamcmd import SteamCMD

router = APIRouter(prefix="/api", tags=["dashboard"])

# ── lightweight in-memory cache for dashboard stats ───────────────
# Keyed by mods_dir so switching config directories doesn't reuse stale data.
_cache: dict[str, dict[str, Any]] = {}
_CACHE_TTL = 5  # seconds


def _cached_dashboard(cfg: Config) -> dict:
    """Return cached dashboard stats if fresh, otherwise recompute."""
    now = time.time()
    key = str(cfg.mods_dir)
    entry = _cache.get(key)
    if entry and now - entry.get("_ts", 0) < _CACHE_TTL:
        return entry

    mods_count = 0
    total_size = 0
    if cfg.mods_dir.exists():
        for d in cfg.mods_dir.iterdir():
            if d.is_dir():
                mods_count += 1
                try:
                    with os.scandir(d) as entries:
                        for e in entries:
                            if e.is_file():
                                total_size += e.stat().st_size
                except OSError:
                    pass

    entry = {
        "mods_count": mods_count,
        "disk_usage_mb": round(total_size / 1024 / 1024, 1),
        "_ts": now,
    }
    _cache[key] = entry
    return entry


@router.get("/dashboard")
async def dashboard(
    cfg: Config = Depends(get_config), au: AutoUpdateManager = Depends(get_autoupdate)
):
    """Dashboard stats: mod count, update status, disk usage, recent activity.

    Update checks are *not* auto-triggered here — they are only started by the
    explicit "一键更新" button (POST /api/auto-update/run), so merely opening
    the dashboard can never kick off a full re-download.
    """
    cached = _cached_dashboard(cfg)
    history = get_download_history(limit=10)
    return {
        "mods_count": cached["mods_count"],
        "updates_pending": len(au.last_result),
        "disk_usage_mb": cached["disk_usage_mb"],
        "recent_activity": history,
    }


@router.get("/steamcmd/check")
def steamcmd_check(cfg: Config = Depends(get_config)):
    """Verify SteamCMD is functional."""
    if not cfg.steamcmd_path.exists():
        return {"ok": False, "msg": "SteamCMD 路径不存在"}
    try:
        steamcmd = SteamCMD(cfg.steamcmd_path)
        result = steamcmd.workshop_download("0")
        for line in result.output_lines:
            if "FAILED" in line and "login" in line.lower():
                return {"ok": False, "msg": "SteamCMD 登录失败"}
        return {"ok": True, "msg": "SteamCMD 就绪"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}
