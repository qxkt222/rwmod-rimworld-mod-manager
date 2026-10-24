"""Mods router — listing, health, compatibility, export, collection-export, disk usage."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.errors import ModNotFoundError, ValidationError
from rwmod.metadata import package_id_key, read_mod_metadata
from rwmod.mod_cache import get_cached_mods
from rwmod.mod_health_check import get_health
from rwmod.mod_sizes import get_sizes, is_pending
from rwmod.models.schemas import DownloadRequest, FoldersRequest
from rwmod.routers.download import _dl_semaphore
from rwmod.utils import extract_mod_id, read_upload_limited
from rwmod.workshop import check_mod_updates

router = APIRouter(prefix="/api/mods", tags=["mods"])


class ActiveModsRequest(BaseModel):
    """POST /api/mods/active — batch enable/disable of packageIds.

    ``sort`` is opt-in: by default enabling appends at the end and never
    reorders the existing load order.
    """

    enable: list[str] = Field(default_factory=list)
    disable: list[str] = Field(default_factory=list)
    sort: bool = False


# Local mod zips are far larger than text uploads — a 2GB cap still stops
# unbounded uploads without rejecting legitimate mods.
_MAX_MOD_ZIP_BYTES = 2 * 1024 * 1024 * 1024


def _reject_oversized_mod(file: UploadFile) -> None:
    """Reject mod-zip uploads larger than the cap without buffering the body."""
    if file.size is not None and file.size > _MAX_MOD_ZIP_BYTES:
        raise HTTPException(413, f"文件过大（上限 {_MAX_MOD_ZIP_BYTES // (1024 * 1024)} MB）")


# ── lightweight in-memory cache ───────────────────────────────────
# Keyed by mods_dir so switching config directories doesn't reuse stale data.
# Only the cheap part (metadata + tags) is cached here; disk sizes come from
# rwmod.mod_sizes' long-lived background snapshot and are overlaid on every
# response. The old code walked every mod folder recursively on each miss
# (5-8 seconds on a 150k-file tree).
_mods_cache: dict[str, dict[str, Any]] = {}
_CACHE_TTL = 30  # seconds


def _cached_mod_list(cfg: Config) -> list[dict]:
    now = time.time()
    key = str(cfg.mods_dir)
    entry = _mods_cache.get(key)
    if entry and now - entry.get("_ts", 0) < _CACHE_TTL:
        rows = entry.get("data")
        if isinstance(rows, list):
            return _with_sizes(cfg, rows)
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
        data.append(
            {
                "folder": m.folder,
                "name": m.name,
                "package_id": m.package_id,
                "workshop_id": m.workshop_id,
                "tags": tags_by_folder.get(m.folder, []),
            }
        )
    _mods_cache[key] = {"data": data, "_ts": now}
    return _with_sizes(cfg, data)


def _with_sizes(cfg: Config, rows: list[dict]) -> list[dict]:
    """Overlay the current size snapshot; never blocks on a disk walk.

    A cold snapshot yields ``size_mb=0`` plus ``size_pending=True`` for the
    first few seconds after startup (the frontend does not display this field;
    it exists for API consumers and tests).
    """
    sizes = get_sizes(cfg.mods_dir)
    pending = is_pending(cfg.mods_dir)
    return [
        {
            **row,
            "size_mb": round(sizes.get(row["folder"], 0) / (1024 * 1024), 2),
            "size_pending": pending,
        }
        for row in rows
    ]


def _active_package_keys(cfg: Config) -> set[str]:
    """Normalized packageIds currently listed in ModsConfig.xml's activeMods.

    Read-only: a missing or malformed ModsConfig degrades to "nothing active"
    rather than failing the whole listing. ``SandboxViolationError`` is
    deliberately not caught — a sandbox escape must fail closed, not look like
    an empty config.
    """
    from rwmod.modsconfig_edit import read_active_mods
    from rwmod.profile import resolve_modsconfig_path

    path = resolve_modsconfig_path(cfg.rimworld_dir, cfg.config_dir)
    if path is None:
        return set()
    try:
        return {key for pid in read_active_mods(path) if (key := package_id_key(pid))}
    except (OSError, ValidationError):
        return set()


@router.get("")
def list_mods(
    cfg: Config = Depends(get_config),
):
    active_keys = _active_package_keys(cfg)
    result = []
    for row in _cached_mod_list(cfg):
        key = package_id_key(row.get("package_id"))
        result.append({**row, "active": bool(key and key in active_keys)})
    return result


@router.post("/active")
def set_active_mods(
    payload: ActiveModsRequest,
    cfg: Config = Depends(get_config),
):
    """Batch enable/disable packageIds in ModsConfig.xml.

    Enabling appends to the end of ``activeMods``; disabling filters entries
    out. Existing order is never changed unless ``sort=true`` is requested.
    The file is backed up (``.bak_<ts>``) and snapshotted for undo before an
    atomic replace.
    """
    from rwmod.metadata import installed_package_ids
    from rwmod.modsconfig_edit import (
        active_order_needs_sort,
        plan_active_changes,
        read_active_mods,
        read_modsconfig_text,
        rewrite_active_mods,
        write_modsconfig_target,
    )
    from rwmod.profile import modsconfig_target

    if not payload.enable and not payload.disable and not payload.sort:
        raise ValidationError("需要提供 enable 或 disable（或 sort=true）")

    # The only ModsConfig write path — profile.modsconfig_target applies the
    # sandbox guard before anything is read or written.
    target = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    if not target.exists():
        raise ModNotFoundError(f"ModsConfig.xml 未找到: {target}")

    current = read_active_mods(target)
    plan = plan_active_changes(
        current,
        enable=payload.enable,
        disable=payload.disable,
        installed=list(installed_package_ids(cfg.mods_dir)),
    )

    new_ids = plan.active
    if payload.sort:
        from rwmod.rimsort import sort_mods

        sorted_result = sort_mods(cfg.mods_dir, new_ids)
        new_ids = [*sorted_result["sorted"], *sorted_result["unresolved"]]

    new_text = rewrite_active_mods(read_modsconfig_text(target), new_ids)
    backup = write_modsconfig_target(target, new_text, label="batch-active")

    return {
        "ok": True,
        "active_count": len(new_ids),
        "added": plan.added,
        "removed": plan.removed,
        "skipped": plan.skipped,
        "backup": backup.name if backup else None,
        "needs_sort": active_order_needs_sort(new_ids),
    }


@router.get("/check-updates")
def check_updates(
    cfg: Config = Depends(get_config),
):
    return {"updates": check_mod_updates(str(cfg.mods_dir))}


# ── health (persistent cache — hits Steam API for 600+ mods) ──────
# mod_health_check keeps workshop time_updated in the existing mod_cache table
# (6h TTL) and refreshes it in a daemon thread behind a single-flight guard.
# Readers never wait for the network; a failed refresh keeps the old status
# instead of mass-marking every mod "removed".
@router.get("/health")
def mod_health(
    cfg: Config = Depends(get_config),
):
    return get_health(cfg.mods_dir)


@router.get("/export")
def export_mods(
    cfg: Config = Depends(get_config),
):
    from datetime import datetime

    if not cfg.mods_dir.exists():
        return {"mods": []}
    mods = []
    for m in get_cached_mods(cfg.mods_dir):
        mods.append(
            {
                "folder": m.folder,
                "name": m.name,
                "package_id": m.package_id,
                "workshop_id": m.workshop_id,
            }
        )
    return {"exported_at": datetime.now().isoformat(), "total": len(mods), "mods": mods}


@router.get("/export-collection")
def export_collection(
    cfg: Config = Depends(get_config),
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
):
    from rwmod.version_support import check_version_support, detect_rimworld_version

    rw_ver = detect_rimworld_version(cfg.rimworld_dir)
    if not rw_ver:
        # Not an error: the request succeeded, the game version simply isn't
        # detectable here. A 200 body carries data only — no error text — so the
        # client decides how to present "no version known".
        return {"rimworld_version": None, "groups": {}}
    metas = get_cached_mods(cfg.mods_dir)
    groups = check_version_support(metas, rw_ver)
    return {"rimworld_version": rw_ver, "groups": groups}


@router.post("/import-local")
async def import_local_mod(
    file: UploadFile = File(...),
    cfg: Config = Depends(get_config),
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
        # API-7: a corrupt / hostile zip raises BadZipFile (documented); it
        # must become the standard 400 error body, not an unhandled 500.
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(content), "r") as zf:
                safe_extract_zip(zf, tmp_dir)
        except (zipfile.BadZipFile, ValueError) as e:
            raise ValidationError(f"压缩包损坏或不安全: {e}") from None

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
    payload: FoldersRequest,
    cfg: Config = Depends(get_config),
):
    """Delete multiple mod folders. Each is backed up to backup_dir first."""
    import shutil

    from rwmod.backup import backup_mod

    folders: list[str] = payload.folders
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
    payload: DownloadRequest,
    cfg: Config = Depends(get_config),
):
    """Download multiple mods by workshop ID (or collection URL).

    Uses the batch pipeline: one SteamCMD process per batch of mods (one
    login) instead of a process per mod; collection ids are detected and
    their children downloaded too.
    """
    from rwmod.downloader import download_batch

    ids: list[str] = payload.ids
    force: bool = payload.force
    parsed = [mid for raw in ids if (mid := extract_mod_id(raw))]
    if not parsed:
        raise HTTPException(400, "没有有效的 Mod ID")
    cfg.validate()

    # Route through the shared global semaphore so batch + queue + SSE streams
    # together never exceed the SteamCMD concurrency budget (Steam rate-limits
    # parallel anonymous logins).
    # Explicit dict[str, bool]: download_batch returns BatchResult (a dict
    # subclass carrying .outcomes), but the failure path below assigns a plain
    # dict — the annotation keeps both branches type-compatible for mypy.
    ok_map: dict[str, bool]
    async with _dl_semaphore:
        try:
            ok_map = await asyncio.to_thread(download_batch, cfg, parsed, force=force)
        except Exception as e:  # noqa: BLE001 — never crash the batch endpoint
            from rwmod.logger import get_log

            get_log("rwmod.mods").error("批量下载异常: %s", e, exc_info=True)
            ok_map = {mid: False for mid in parsed}

    results = [{"id": mid, "ok": ok_map.get(mid, False)} for mid in parsed]
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
):
    """Report which mods ship a Chinese translation (汉化)."""
    if not cfg.mods_dir.exists():
        return {"mods": []}
    metas = [m for m in get_cached_mods(cfg.mods_dir) if m.folder]

    # Scan the Languages/ dirs in parallel — folder stat + iterdir per mod
    # adds up when the Mods dir holds hundreds of entries.
    def _check(m) -> dict:
        return {
            "folder": m.folder,
            "name": m.name,
            "workshop_id": m.workshop_id,
            "has_chinese": _detect_chinese(cfg.mods_dir / m.folder),
        }

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(metas)))) as ex:
        results = list(ex.map(_check, metas))
    translated = sum(1 for r in results if r["has_chinese"])
    return {"total": len(results), "translated": translated, "mods": results}
