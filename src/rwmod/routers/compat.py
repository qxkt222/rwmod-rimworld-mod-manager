"""Mod conflict / dependency check router (HTTP surface: /api/compat)."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.mod_conflicts import find_mod_conflicts

router = APIRouter(prefix="/api/compat", tags=["compat"])


@router.get("/check")
def compat_check(
    cfg: Config = Depends(get_config),
):
    """Check installed mods for missing dependencies and conflicts."""
    result = find_mod_conflicts(cfg.mods_dir)

    # Resolve missing dependency packageIds to workshop IDs where possible,
    # so the frontend can offer a one-click download.
    missing_pids = sorted(
        {dep for entry in result["missing_dependencies"] for dep in entry["missing"]}
    )
    if missing_pids:
        from rwmod.rimsort import resolve_missing_workshop_ids

        result["missing_details"] = resolve_missing_workshop_ids(missing_pids, cfg.mods_dir)
    else:
        result["missing_details"] = []

    return result
