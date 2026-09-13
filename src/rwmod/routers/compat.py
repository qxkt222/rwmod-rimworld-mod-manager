"""Compatibility check router — missing dependencies & conflicts."""

from fastapi import APIRouter, Depends

from rwmod.compat import check_compatibility
from rwmod.config import Config
from rwmod.deps import get_config

router = APIRouter(prefix="/api/compat", tags=["compat"])


@router.get("/check")
def compat_check(
    cfg: Config = Depends(get_config),
):
    """Check installed mods for missing dependencies and conflicts."""
    result = check_compatibility(cfg.mods_dir)

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
