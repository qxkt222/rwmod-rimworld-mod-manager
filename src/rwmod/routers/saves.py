"""Save file analysis router \u2014 parse .rws files to detect mod requirements."""

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from rwmod.auth import get_current_user
from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.parser import get_installed_package_ids
from rwmod.save_parser import analyze_save, find_save_files, parse_save_mods

router = APIRouter(prefix="/api/saves", tags=["saves"])


@router.get("")
def list_saves(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """List all found save files with basic analysis."""
    saves = find_save_files(cfg.rimworld_dir)
    installed = get_installed_package_ids(cfg.mods_dir)
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


@router.get("/{save_name}")
def save_detail(
    save_name: str,
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Analyze a specific save file in detail."""
    saves = find_save_files(cfg.rimworld_dir)
    target = next((sp for sp in saves if sp.stem == save_name or sp.name == save_name), None)
    if target is None:
        return {"error": f"Save not found: {save_name}"}
    installed = get_installed_package_ids(cfg.mods_dir)
    return analyze_save(target, installed)


@router.post("/analyze")
async def api_upload_and_analyze(
    file: UploadFile,
    _user: str = Depends(get_current_user),
):
    """Upload a .rws save file and get its mod requirements."""
    from rwmod.routers.download import _reject_oversized

    _reject_oversized(file)
    try:
        content = (await file.read()).decode("utf-8", errors="replace")
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
    _user: str = Depends(get_current_user),
):
    """Download all mods that a save requires but are not installed.

    Missing packageIds are resolved to workshop IDs (best-effort) and each
    resolved mod is downloaded via SteamCMD in a background thread.
    """
    import asyncio

    from rwmod.downloader import download_one
    from rwmod.parser import resolve_workshop_ids

    saves = find_save_files(cfg.rimworld_dir)
    target = next((sp for sp in saves if sp.stem == save_name or sp.name == save_name), None)
    if target is None:
        raise HTTPException(404, f"Save not found: {save_name}")

    installed = get_installed_package_ids(cfg.mods_dir)
    analysis = analyze_save(target, installed)
    missing = analysis["missing_mods"]
    if not missing:
        return {"total": 0, "results": [], "msg": "该存档所需 Mod 已全部安装"}

    cfg.validate()
    known, unknown = resolve_workshop_ids(missing, cfg.mods_dir)
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
