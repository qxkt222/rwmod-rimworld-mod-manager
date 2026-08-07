"""Mods router — listing, health, compatibility, export, collection-export, disk usage."""

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from rwmod.auth import get_current_user
from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.metadata import read_mod_metadata
from rwmod.mod_cache import get_cached_mods
from rwmod.routers.download import _bounded_download
from rwmod.utils import read_upload_limited
from rwmod.workshop import check_mod_updates, fetch_item_details

router = APIRouter(prefix="/api/mods", tags=["mods"])

# Local mod zips are far larger than text uploads — a 2GB cap still stops
# unbounded uploads without rejecting legitimate mods.
_MAX_MOD_ZIP_BYTES = 2 * 1024 * 1024 * 1024


def _reject_oversized_mod(file: UploadFile) -> None:
    """Reject mod-zip uploads larger than the cap without buffering the body."""
    if file.size is not None and file.size > _MAX_MOD_ZIP_BYTES:
        raise HTTPException(413, f"文件过大（上限 {_MAX_MOD_ZIP_BYTES // (1024 * 1024)} MB）")


# ── lightweight in-memory cache ───────────────────────────────────
# Keyed by mods_dir so switching config directories doesn't reuse stale data.
# Each miss walks EVERY mod folder recursively for disk usage — the UI polls
# faster than a short TTL, so 30s amortizes the scan across requests.
_mods_cache: dict[str, dict[str, Any]] = {}
_CACHE_TTL = 30  # seconds


def _cached_mod_list(cfg: Config) -> list[dict]:
    now = time.time()
    key = str(cfg.mods_dir)
    entry = _mods_cache.get(key)
    if entry and now - entry.get("_ts", 0) < _CACHE_TTL:
        data = entry.get("data")
        if isinstance(data, list):
            return data
    if not cfg.mods_dir.exists():
        return []
    metas = get_cached_mods(cfg.mods_dir)

    # Batch-load all folder→tags mappings in ONE query (avoids N+1
    # per-mod get_tags() calls — a massive speedup for hundreds of mods).
    from rwmod.database import get_conn

    db = get_conn()
    rows = db.execute("SELECT folder, tag FROM mod_tags ORDER BY folder, tag").fetchall()
    tags_by_folder: dict[str, list[str]] = {}
    for r in rows:
        tags_by_folder.setdefault(r["folder"], []).append(r["tag"])

    data = []
    for m in metas:
        mod_dir = cfg.mods_dir / m.folder
        size_mb = _get_dir_size_mb(mod_dir)
        tags = tags_by_folder.get(m.folder, [])
        data.append(
            {
                "folder": m.folder,
                "name": m.name,
                "package_id": m.package_id,
                "workshop_id": m.workshop_id,
                "size_mb": size_mb,
                "tags": tags,
            }
        )
    _mods_cache[key] = {"data": data, "_ts": now}
    return data


def _get_dir_size_mb(dir_path) -> float:
    """Get the total file size of a directory in MB.

    Uses os.walk for a *recursive* scan — mods contain nested folders
    (About/, Defs/, Assemblies/, Patches/, ...) so a top-level os.scandir
    alone would badly under-report disk usage.
    """
    total = 0
    try:
        for root, _dirs, files in os.walk(dir_path):
            for fname in files:
                try:
                    total += os.path.getsize(os.path.join(root, fname))
                except OSError:
                    continue
    except OSError:
        pass
    return round(total / 1024 / 1024, 2)


@router.get("")
def list_mods(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    return _cached_mod_list(cfg)


@router.get("/check-updates")
def check_updates(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    return {"updates": check_mod_updates(str(cfg.mods_dir))}


# ── health cache (expensive — hits Steam API for 600+ mods) ───────
# Keyed by mods_dir so switching config directories returns correct data.
_health_cache: dict[str, dict[str, Any]] = {}
_HEALTH_CACHE_TTL = 60  # 1 minute — Steam API data changes slowly


@router.get("/health")
def mod_health(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    now = time.time()
    key = str(cfg.mods_dir)
    entry = _health_cache.get(key)
    if entry and now - entry.get("_ts", 0) < _HEALTH_CACHE_TTL:
        return entry["data"]

    if not cfg.mods_dir.exists():
        return {"mods": []}
    metas = [
        m
        for m in (read_mod_metadata(d) for d in sorted(cfg.mods_dir.iterdir()))
        if m and m.workshop_id
    ]
    all_ids = [m.workshop_id for m in metas]
    details = fetch_item_details(all_ids)
    results = []
    for meta in metas:
        remote = details.get(meta.workshop_id)
        if remote:
            age = (now - remote.get("time_updated", 0)) / 86400
            status = "maintained" if age < 90 else "stale" if age < 365 else "abandoned"
            last_updated = time.strftime("%Y-%m-%d", time.gmtime(remote["time_updated"]))
        else:
            status, last_updated = "removed", ""
        results.append(
            {
                "folder": meta.folder,
                "name": meta.name,
                "workshop_id": meta.workshop_id,
                "status": status,
                "last_updated": last_updated,
            }
        )
    data = {"mods": results}
    _health_cache[key] = {"data": data, "_ts": now}
    return data


@router.get("/export")
def export_mods(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    from datetime import datetime

    if not cfg.mods_dir.exists():
        return {"mods": []}
    mods = []
    for d in sorted(cfg.mods_dir.iterdir()):
        meta = read_mod_metadata(d)
        if meta:
            mods.append(
                {
                    "folder": meta.folder,
                    "name": meta.name,
                    "package_id": meta.package_id,
                    "workshop_id": meta.workshop_id,
                }
            )
    return {"exported_at": datetime.now().isoformat(), "total": len(mods), "mods": mods}


@router.get("/export-collection")
def export_collection(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    metas = get_cached_mods(cfg.mods_dir)
    mods = []
    ids_only: list[str] = []
    for m in metas:
        if m.workshop_id:
            url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={m.workshop_id}"
            mods.append({"workshop_id": m.workshop_id, "name": m.name, "url": url})
            ids_only.append(m.workshop_id)
    header = f"# RimWorld Mod 合集 — {len(mods)} 个 Mod"
    lines = [header, "", "## Workshop ID 列表", "", " ".join(ids_only), ""]
    return {"total": len(mods), "mods": mods, "ids": ids_only, "markdown": "\n".join(lines)}


@router.get("/compatibility")
def mod_compatibility(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    from rwmod.compatibility import check_compatibility, detect_rimworld_version

    rw_ver = detect_rimworld_version(cfg.rimworld_dir)
    if not rw_ver:
        return {"error": "未能检测到 RimWorld 版本", "rimworld_version": None, "groups": {}}
    metas = get_cached_mods(cfg.mods_dir)
    groups = check_compatibility(metas, rw_ver)
    return {"rimworld_version": rw_ver, "groups": groups}


@router.post("/import-local")
async def import_local_mod(
    file: UploadFile = File(...),
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Import a local mod from a zip file.

    The zip may contain either a single mod folder (with About/About.xml) or
    multiple mod folders. Each folder is validated for a readable About.xml
    before being extracted into mods_dir.
    """

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "请上传 .zip 文件")
    _reject_oversized_mod(file)

    content = await read_upload_limited(file, _MAX_MOD_ZIP_BYTES)
    if content is None:
        raise HTTPException(413, f"文件过大（上限 {_MAX_MOD_ZIP_BYTES // (1024 * 1024)} MB）")
    if not content:
        raise HTTPException(400, "文件为空")

    # Decompression + copy is CPU/disk heavy — run it off the event loop so a
    # multi-GB import can never freeze the server.
    return await asyncio.to_thread(_import_local_sync, content, cfg)


def _import_local_sync(content: bytes, cfg: Config) -> dict:
    """Validate + extract an uploaded mod zip (blocking; run in a thread)."""
    import shutil
    import tempfile
    import zipfile
    from pathlib import Path

    from rwmod.utils import safe_extract_zip

    cfg.mods_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="rwmod_import_"))
    try:
        # Extract to a temp dir first so we can validate before moving.
        with zipfile.ZipFile(__import__("io").BytesIO(content), "r") as zf:
            safe_extract_zip(zf, tmp_dir)

        # Determine candidate mod folders: top-level dirs that contain About.xml.
        candidates: list[Path] = []
        for entry in sorted(tmp_dir.iterdir()):
            if entry.is_dir():
                if (entry / "About" / "About.xml").exists():
                    candidates.append(entry)
                else:
                    # Maybe the zip wraps everything in a single root folder.
                    for sub in sorted(entry.iterdir()):
                        if sub.is_dir() and (sub / "About" / "About.xml").exists():
                            candidates.append(sub)
            elif entry.name == "About.xml":
                # A bare About.xml at the root — treat the whole tmp as one mod.
                candidates.append(tmp_dir)
                break

        if not candidates:
            raise HTTPException(400, "压缩包内未找到有效的 Mod（缺少 About/About.xml）")

        imported = []
        for cand in candidates:
            meta = read_mod_metadata(cand)
            if meta is None:
                continue
            dest = cfg.mods_dir / meta.folder
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(cand, dest)
            imported.append(
                {
                    "folder": meta.folder,
                    "name": meta.name,
                    "package_id": meta.package_id,
                    "workshop_id": meta.workshop_id,
                }
            )

        if not imported:
            raise HTTPException(400, "未能识别压缩包内的 Mod 元数据")

        return {"ok": True, "imported": imported, "count": len(imported)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/batch/delete")
def batch_delete_mods(
    payload: dict,
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Delete multiple mod folders. Each is backed up to backup_dir first."""
    import shutil

    from rwmod.backup import backup_mod

    folders: list[str] = payload.get("folders", [])
    if not folders:
        raise HTTPException(400, "需要提供要删除的 Mod 文件夹列表")

    cfg.mods_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)

    deleted = []
    failed = []
    for folder in folders:
        # Path-traversal guard: only allow plain folder names.
        if not folder or folder in (".", "..") or "/" in folder or "\\" in folder:
            failed.append({"folder": folder, "reason": "非法文件夹名"})
            continue
        target = cfg.mods_dir / folder
        if not target.is_dir():
            failed.append({"folder": folder, "reason": "不存在"})
            continue

        meta = read_mod_metadata(target)
        workshop_id = meta.workshop_id if meta else ""
        backup_mod(cfg.mods_dir, workshop_id, folder, cfg.backup_dir)
        shutil.rmtree(target)
        deleted.append(folder)

    return {"ok": True, "deleted": deleted, "failed": failed, "count": len(deleted)}


@router.post("/batch/download")
async def batch_download_mods(
    payload: dict,
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Download multiple mods by workshop ID (or collection URL)."""
    from rwmod.downloader import extract_mod_id

    ids: list[str] = payload.get("ids", [])
    force: bool = payload.get("force", False)
    parsed = [mid for raw in ids if (mid := extract_mod_id(raw))]
    if not parsed:
        raise HTTPException(400, "没有有效的 Mod ID")
    cfg.validate()

    # Route through the shared global semaphore so batch + queue + SSE streams
    # together never exceed the SteamCMD concurrency budget (Steam rate-limits
    # parallel anonymous logins).
    results = []
    for mid in parsed:
        ok = await _bounded_download(cfg, mid, force)
        results.append({"id": mid, "ok": ok})

    return {"total": len(results), "results": results}


# ── localization (汉化) detection ─────────────────────────────────
# RimWorld mods ship translations under Languages/<Lang>/Keyed/*.xml.
# Chinese is usually "ChineseSimplified" or "Chinese".
_CHINESE_LANG_DIRS = {"chinesesimplified", "chinese", "简体中文", "简体", "中文"}


def _detect_chinese(mod_dir) -> bool:
    """Return True if the mod ships a Chinese translation folder."""
    lang_dir = mod_dir / "Languages"
    if not lang_dir.is_dir():
        return False
    try:
        for entry in lang_dir.iterdir():
            if entry.is_dir() and entry.name.lower() in _CHINESE_LANG_DIRS:
                return True
    except OSError:
        return False
    return False


@router.get("/localization")
def localization_status(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Report which mods ship a Chinese translation (汉化)."""
    if not cfg.mods_dir.exists():
        return {"mods": []}
    results = []
    for d in sorted(cfg.mods_dir.iterdir()):
        if not d.is_dir():
            continue
        meta = read_mod_metadata(d)
        if meta is None:
            continue
        results.append(
            {
                "folder": meta.folder,
                "name": meta.name,
                "workshop_id": meta.workshop_id,
                "has_chinese": _detect_chinese(d),
            }
        )
    translated = sum(1 for r in results if r["has_chinese"])
    return {"total": len(results), "translated": translated, "mods": results}
