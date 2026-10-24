"""Dashboard router."""

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends

from rwmod.autoupdate import AutoUpdateManager
from rwmod.config import Config
from rwmod.database import get_download_history
from rwmod.deps import get_autoupdate, get_config
from rwmod.mod_cache import get_cached_mods
from rwmod.mod_health_check import get_health
from rwmod.mod_sizes import get_sizes, is_pending

router = APIRouter(prefix="/api", tags=["dashboard"])

# ── lightweight in-memory cache for dashboard stats ───────────────
# Keyed by mods_dir so switching config directories doesn't reuse stale data.
_cache: dict[str, dict[str, Any]] = {}
_CACHE_TTL = 5  # seconds


def _cached_dashboard(cfg: Config) -> dict:
    """Return cached dashboard stats if fresh, otherwise recompute.

    Counts and sizes come from the metadata cache + mod_sizes snapshot; the
    old implementation walked all ~150k files with ``Path.stat()`` on every
    call (7-18 seconds). ``backup_dir`` is excluded: it commonly lives inside
    ``mods_dir`` (``_backups``) and must not inflate the mod count or usage.
    """
    now = time.time()
    key = str(cfg.mods_dir)
    entry = _cache.get(key)
    if entry and now - entry.get("_ts", 0) < _CACHE_TTL:
        return entry

    mods_count = 0
    total_bytes = 0
    pending = False
    if cfg.mods_dir.exists():
        metas = get_cached_mods(cfg.mods_dir)
        mods_count = len(metas)
        sizes = get_sizes(cfg.mods_dir)
        pending = is_pending(cfg.mods_dir)
        excluded = {cfg.backup_dir.name} if cfg.backup_dir.parent == cfg.mods_dir else set()
        total_bytes = sum(sizes.get(m.folder, 0) for m in metas if m.folder not in excluded)

    entry = {
        "mods_count": mods_count,
        "disk_usage_mb": round(total_bytes / (1024 * 1024), 1),
        "size_pending": pending,
        "_ts": now,
    }
    _cache[key] = entry
    return entry


@router.get("/dashboard")
async def dashboard(
    cfg: Config = Depends(get_config),
    au: AutoUpdateManager = Depends(get_autoupdate),
):
    """Dashboard stats: mod count, update status, disk usage, recent activity.

    Update checks are *not* auto-triggered here — they are only started by the
    explicit "一键更新" button (POST /api/auto-update/run), so merely opening
    the dashboard can never kick off a full re-download.
    """
    # Stats come from caches only (metadata + size snapshot) — run off the
    # event loop so a cold metadata scan can never freeze the server.
    cached = await asyncio.to_thread(_cached_dashboard, cfg)
    history = get_download_history(limit=10)

    # Health counts come from the persistent cache. ``start_refresh=False`` is
    # deliberate: a dashboard refresh must never trigger Steam API work. The
    # mods panel's /api/mods/health call owns the background refresh.
    abandoned = stale = removed = 0
    health_stale = False
    try:
        health = await asyncio.to_thread(get_health, cfg.mods_dir, start_refresh=False)
        health_stale = bool(health.get("stale"))
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
        "size_pending": cached.get("size_pending", False),
        "recent_activity": history,
        "health": {
            "abandoned": abandoned,
            "stale": stale,
            "removed": removed,
            "stale_data": health_stale,
        },
    }


@router.get("/steamcmd/check")
def steamcmd_check(
    cfg: Config = Depends(get_config),
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
    # SteamCMD on Windows refuses to run from a path containing non-ASCII
    # characters; the old check said "就绪" because the fatal message happened
    # to contain the word "steam".
    if any(ord(ch) > 127 for ch in str(cfg.steamcmd_path)):
        return {
            "ok": False,
            "msg": (
                f"SteamCMD 路径含非 ASCII 字符，SteamCMD 无法运行：{cfg.steamcmd_path}。"
                "请把 SteamCMD 移到纯英文路径（例如 D:\\steamcmd）并在设置中更新路径。"
            ),
        }
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
        if "incompatible path" in out or "非 ascii" in out or "非ascii" in out:
            return {
                "ok": False,
                "msg": (
                    f"SteamCMD 无法从路径 {cfg.steamcmd_path.parent} 运行"
                    "（路径含非 ASCII 字符）。请移到纯英文路径并更新设置。"
                ),
            }
        if "steam console client" in out or "success" in out or proc.returncode in (0, 7):
            # returncode 7 is SteamCMD's "already running / need update then retry"
            # code which still proves the binary is functional.
            return {"ok": True, "msg": "SteamCMD 就绪"}
        return {"ok": False, "msg": f"SteamCMD 异常退出 (code {proc.returncode})"}
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        return {"ok": False, "msg": str(e)}
