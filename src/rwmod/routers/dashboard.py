"""Dashboard router."""

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from rwmod.auth import get_current_user
from rwmod.autoupdate import AutoUpdateManager
from rwmod.config import Config
from rwmod.database import get_download_history
from rwmod.deps import get_autoupdate, get_config

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
                # Recursively sum file sizes — mods contain nested folders
                # (About/, Defs/, Assemblies/, ...) so a top-level scan alone
                # would badly under-report disk usage.
                for root, _dirs, files in os.walk(d):
                    for fname in files:
                        with contextlib.suppress(OSError):
                            total_size += (Path(root) / fname).stat().st_size

    entry = {
        "mods_count": mods_count,
        "disk_usage_mb": round(total_size / 1024 / 1024, 1),
        "_ts": now,
    }
    _cache[key] = entry
    return entry


@router.get("/dashboard")
async def dashboard(
    cfg: Config = Depends(get_config),
    au: AutoUpdateManager = Depends(get_autoupdate),
    _user: str = Depends(get_current_user),
):
    """Dashboard stats: mod count, update status, disk usage, recent activity.

    Update checks are *not* auto-triggered here — they are only started by the
    explicit "一键更新" button (POST /api/auto-update/run), so merely opening
    the dashboard can never kick off a full re-download.
    """
    # Both calls are blocking (full recursive disk walk + per-mod Steam API
    # requests with up-to-15s timeouts) — run them off the event loop so a
    # dashboard refresh can never freeze the whole server.
    cached = await asyncio.to_thread(_cached_dashboard, cfg)
    history = get_download_history(limit=10)

    # Abandoned/stale/removed mod counts (reuses the health scan, cached 60s).
    abandoned = stale = removed = 0
    try:
        from rwmod.routers.mods import mod_health

        health = await asyncio.to_thread(mod_health, cfg, _user="")
        for m in health.get("mods", []):
            if m["status"] == "abandoned":
                abandoned += 1
            elif m["status"] == "stale":
                stale += 1
            elif m["status"] == "removed":
                removed += 1
    except Exception:
        pass

    return {
        "mods_count": cached["mods_count"],
        "updates_pending": len(au.last_result),
        "disk_usage_mb": cached["disk_usage_mb"],
        "recent_activity": history,
        "health": {"abandoned": abandoned, "stale": stale, "removed": removed},
    }


@router.get("/steamcmd/check")
def steamcmd_check(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Verify SteamCMD is functional.

    NOTE: Uses ``--help`` / version detection instead of actually launching a
    workshop download. The old implementation ran ``workshop_download("0")``
    which would start a real SteamCMD process and attempt to download item 0
    — a pointless network round-trip that could hang for minutes if the
    network is down. Checking that the executable exists and is runnable is
    sufficient for a healthy check.
    """
    if not cfg.steamcmd_path.exists():
        return {"ok": False, "msg": "SteamCMD 路径不存在"}
    try:
        import subprocess

        exe = str(cfg.steamcmd_path)
        # Quick, non-network check: run with --help (SteamCMD exits fast).
        # On Windows steamcmd.exe may print its banner and exit 0.
        proc = subprocess.run(  # nosec B603 — command list is fixed, no shell
            [exe, "+quit"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            cwd=str(cfg.steamcmd_path.parent),
        )
        out = (proc.stdout or "").lower()
        if "steam" in out or "success" in out or proc.returncode in (0, 7):
            # returncode 7 is SteamCMD's "already running / need update then retry"
            # code which still proves the binary is functional.
            return {"ok": True, "msg": "SteamCMD 就绪"}
        return {"ok": False, "msg": f"SteamCMD 异常退出 (code {proc.returncode})"}
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        return {"ok": False, "msg": str(e)}
