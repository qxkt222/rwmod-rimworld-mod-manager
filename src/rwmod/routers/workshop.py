"""Workshop router — search, dependency lookup, collection preview."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.database import get_download_history
from rwmod.deps import get_config
from rwmod.downloader import extract_mod_id
from rwmod.mod_cache import get_cached_mods
from rwmod.workshop import (
    fetch_collection_children,
    fetch_item_details,
    search_workshop,
)

router = APIRouter(prefix="/api", tags=["workshop"])


@router.get("/search")
def search(
    q: str = "",
    page: int = 1,
    cfg: Config = Depends(get_config),
):
    if not q.strip():
        return {"results": []}
    try:
        results = search_workshop(q.strip(), page=page)
    except Exception:
        return {"results": []}
    installed_ids: set[str] = set()
    if cfg.mods_dir.exists():
        for m in get_cached_mods(cfg.mods_dir):
            if m.workshop_id:
                installed_ids.add(m.workshop_id)
    enriched = []
    for r in results:
        d = r.__dict__
        d["installed"] = r.id in installed_ids
        enriched.append(d)
    return {"results": enriched}


@router.get("/workshop/{mod_id}")
def workshop_detail(mod_id: str):
    try:
        details = fetch_item_details([mod_id])
    except Exception:
        return {"found": False}
    if mod_id in details:
        return {"found": True, "detail": details[mod_id]}
    return {"found": False}


@router.get("/collection/preview/{collection_id}")
def collection_preview(
    collection_id: str,
    cfg: Config = Depends(get_config),
):
    cid = extract_mod_id(collection_id) or collection_id
    try:
        mod_ids = fetch_collection_children(cid)
    except Exception as e:
        return {"error": f"获取合集失败: {e}"}
    if not mod_ids:
        return {"error": "未能获取合集内容"}

    # Build the installed workshop-id → folder map with a single metadata scan
    # (avoid re-scanning mods_dir once per child — 593 children × full scan).
    installed_lookup: dict[str, str] = {}
    if cfg.mods_dir.exists():
        for m in get_cached_mods(cfg.mods_dir):
            if m.workshop_id:
                installed_lookup[m.workshop_id] = m.folder

    # Which workshop IDs previously failed (single history query, not per child).
    failed_hist = {h["workshop_id"] for h in get_download_history(limit=100, status="failed")}

    installed: list[dict] = []
    new_mods: list[str] = []
    failed_before: list[str] = []
    for mid in mod_ids:
        if mid in installed_lookup:
            installed.append({"id": mid, "name": installed_lookup[mid]})
        elif mid in failed_hist:
            failed_before.append(mid)
        else:
            new_mods.append(mid)

    return {
        "collection_id": cid,
        "total": len(mod_ids),
        "installed": installed,
        "installed_count": len(installed),
        "new_mods": new_mods,
        "new_count": len(new_mods),
        "failed_before": failed_before,
        "failed_count": len(failed_before),
    }


@router.post("/mods/dependencies")
def mod_dependencies(
    payload: dict,
    cfg: Config = Depends(get_config),
):
    from rwmod.workshop import fetch_item_dependencies

    ids: list[str] = payload.get("ids", [])
    if not ids:
        return {"deps": {}}
    raw_deps = fetch_item_dependencies(ids)
    all_dep_ids: set[str] = set()
    for dep_list in raw_deps.values():
        all_dep_ids.update(dep_list)
    installed: dict[str, str] = {}
    if cfg.mods_dir.exists():
        for m in get_cached_mods(cfg.mods_dir):
            if m.workshop_id:
                installed[m.workshop_id] = m.name
    unknown = [d for d in all_dep_ids if d not in installed]
    remote_names: dict[str, str] = {}
    if unknown:
        details = fetch_item_details(unknown)
        for wid, info in details.items():
            remote_names[wid] = info.get("title", wid)
    result: dict[str, list[dict]] = {}
    for mid in ids:
        dep_ids = raw_deps.get(mid, [])
        result[mid] = [
            {
                "id": did,
                "name": installed.get(did) or remote_names.get(did, did),
                "installed": did in installed,
            }
            for did in dep_ids
        ]
    return {"deps": result}
