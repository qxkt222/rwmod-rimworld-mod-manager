"""Workshop router — search, dependency lookup, collection preview."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.database import get_download_history
from rwmod.deps import get_config
from rwmod.errors import ModNotFoundError, WorkshopError
from rwmod.mod_cache import get_cached_mods
from rwmod.models.schemas import ModIdsRequest, SearchResponse
from rwmod.offline import get_status
from rwmod.utils import extract_mod_id
from rwmod.workshop import (
    fetch_collection_children,
    fetch_item_details,
    search_workshop,
)

router = APIRouter(prefix="/api", tags=["workshop"])

#: A Steam call counts as "just failed" only while the observation is this
#: young. workshop.py's network layer records the outcome of every real
#: request, so a stale offline state from minutes ago never taints a request
#: that was answered from a mock/empty result.
_STEAM_FAULT_WINDOW_SEC = 1.0


def _steam_fault() -> bool:
    """True when the call that just returned nothing failed to reach Steam.

    ``search_workshop`` / ``fetch_item_details`` report a network failure as an
    empty result, so without this check an unreachable Steam API looked exactly
    like "no matching mods" and the UI silently showed an empty list.
    """
    status = get_status()
    if status.get("online", True):
        return False
    try:
        return float(status.get("last_check_ago_sec", 0)) < _STEAM_FAULT_WINDOW_SEC
    except (TypeError, ValueError):  # pragma: no cover — defensive
        return False


@router.get("/search", response_model=SearchResponse)
def search(
    q: str = "",
    page: int = 1,
    cfg: Config = Depends(get_config),
):
    if not q.strip():
        return {"results": []}
    try:
        results = search_workshop(q.strip(), page=page)
    except Exception as e:
        # A Steam/API failure is a 502, never an empty 200 that pretends the
        # catalog has no matches.
        raise WorkshopError(f"Workshop 搜索失败: {e}") from e
    if not results and _steam_fault():
        raise WorkshopError("Steam Workshop 不可用，搜索结果未知")
    installed_ids: set[str] = set()
    if cfg.mods_dir.exists():
        for m in get_cached_mods(cfg.mods_dir):
            if m.workshop_id:
                installed_ids.add(m.workshop_id)
    # Built field by field on purpose: `r.__dict__` would publish every field of
    # ModSearchResult, so a field added for internal reasons would silently become
    # part of the API. SearchResponse pins the same list on the way out.
    enriched = [
        {
            "id": r.id,
            "title": r.title,
            "author": r.author,
            "description": r.description,
            "preview_url": r.preview_url,
            "rating": r.rating,
            "subscribers": r.subscribers,
            "installed": r.id in installed_ids,
        }
        for r in results
    ]
    return {"results": enriched}


@router.get("/workshop/{mod_id}")
def workshop_detail(mod_id: str):
    try:
        details = fetch_item_details([mod_id])
    except Exception as e:
        raise WorkshopError(f"获取 Workshop 详情失败: {e}") from e
    if not details and _steam_fault():
        # "cannot ask" must not be reported as "does not exist".
        raise WorkshopError("Steam Workshop 不可用，无法确认该 Mod")
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
        raise WorkshopError(f"获取合集失败: {e}") from e
    if not mod_ids:
        if _steam_fault():
            raise WorkshopError("Steam Workshop 不可用，未能获取合集内容")
        raise ModNotFoundError("未能获取合集内容")

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
    payload: ModIdsRequest,
    cfg: Config = Depends(get_config),
):
    from rwmod.workshop import fetch_item_dependencies

    ids: list[str] = payload.ids
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
