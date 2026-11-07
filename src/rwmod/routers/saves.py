"""Save file analysis router \u2014 parse .rws files to detect mod requirements."""

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.errors import ModNotFoundError
from rwmod.metadata import ModMeta, package_id_key
from rwmod.mod_cache import get_cached_mods
from rwmod.models.schemas import SaveAnalyzeResponse, SaveDetailResponse, SaveListResponse
from rwmod.save_parser import analyze_save, find_save_files, parse_save_mods

router = APIRouter(prefix="/api/saves", tags=["saves"])


def _installed_package_ids(cfg: Config) -> set[str]:
    """packageIds of installed mods, read from the shared metadata cache.

    Re-parsing every About.xml on each save request dominated the cost on a
    560-mod install; the cache already refreshes via TTL and per-folder mtime.
    """
    return {m.package_id for m in get_cached_mods(cfg.mods_dir) if m.package_id}


def _workshop_ids_by_key(metas: list[ModMeta]) -> dict[str, str]:
    """Map the comparison key of each cached packageId to its workshop ID."""
    return {package_id_key(m.package_id): m.workshop_id for m in metas if m.workshop_id}


@router.get("", response_model=SaveListResponse)
def list_saves(
    cfg: Config = Depends(get_config),
):
    """List all found save files with basic analysis."""
    saves = find_save_files(cfg.rimworld_dir, config_dir=cfg.config_dir)
    installed = _installed_package_ids(cfg)
    results = []
    for sp in saves[:50]:
        analysis = analyze_save(sp, installed)
        results.append(
            {
                "name": analysis["name"],
                "game_version": analysis["game_version"],
                "total_mods": analysis["total_mods"],
                "missing_count": len(analysis["missing_mods"]),
                "loadable": analysis["loadable"],
                "completeness": analysis["completeness"],
            }
        )
    return {"saves": results}


@router.get("/{save_name}", response_model=SaveDetailResponse)
def save_detail(
    save_name: str,
    cfg: Config = Depends(get_config),
):
    """Analyze a specific save file in detail."""
    saves = find_save_files(cfg.rimworld_dir, config_dir=cfg.config_dir)
    target = next((sp for sp in saves if sp.stem == save_name or sp.name == save_name), None)
    if target is None:
        raise ModNotFoundError(f"Save not found: {save_name}")
    installed = _installed_package_ids(cfg)
    return analyze_save(target, installed)


@router.post("/analyze", response_model=SaveAnalyzeResponse)
async def api_upload_and_analyze(
    file: UploadFile,
):
    """Upload a .rws save file and get its mod requirements."""
    from rwmod.routers.download import MAX_UPLOAD_BYTES, _reject_oversized
    from rwmod.utils import read_upload_limited

    _reject_oversized(file)
    # Content-Length is absent on a chunked upload, so the cap must be enforced
    # while reading — a bare `await file.read()` buffered the entire body.
    raw = await read_upload_limited(file, MAX_UPLOAD_BYTES)
    if raw is None:
        raise HTTPException(413, f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024} KB）")
    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(400, f"Cannot read file: {e}") from e
    try:
        mods = parse_save_mods(content)
        return {"filename": file.filename, "mods": mods, "mod_count": len(mods)}
    except Exception as e:
        raise HTTPException(400, f"Failed to parse save: {e}") from e


@router.post("/{save_name}/download-missing")
async def download_missing_mods(
    save_name: str,
    cfg: Config = Depends(get_config),
):
    """Download all mods that a save requires but are not installed.

    Missing packageIds are resolved to workshop IDs (best-effort) and each
    resolved mod is downloaded via SteamCMD in a background thread.
    """
    import asyncio

    from rwmod.downloader import download_one

    saves = find_save_files(cfg.rimworld_dir, config_dir=cfg.config_dir)
    target = next((sp for sp in saves if sp.stem == save_name or sp.name == save_name), None)
    if target is None:
        raise HTTPException(404, f"Save not found: {save_name}")

    # One cache read serves both the installed-id set and the workshop-ID
    # lookup (issue: re-parsing all About.xml twice per request).
    metas = get_cached_mods(cfg.mods_dir)
    installed = {m.package_id for m in metas if m.package_id}
    analysis = analyze_save(target, installed)
    missing = analysis["missing_mods"]
    if not missing:
        return {"total": 0, "results": [], "msg": "该存档所需 Mod 已全部安装"}

    cfg.validate()
    workshop_by_key = _workshop_ids_by_key(metas)
    known: list[str] = []
    unknown: list[str] = []
    for pid in missing:
        wid = workshop_by_key.get(package_id_key(pid), "")
        if wid:
            known.append(wid)
        else:
            unknown.append(pid)
    results = []
    for wid in known:
        ok = await asyncio.to_thread(download_one, cfg, wid)
        results.append({"id": wid, "ok": ok})

    return {
        "total_missing": len(missing),
        "resolved": len(known),
        "unknown": unknown,
        "downloaded": len(results),
        "results": results,
    }
