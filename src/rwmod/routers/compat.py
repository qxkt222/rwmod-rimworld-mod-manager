"""Mod conflict / dependency check router (HTTP surface: /api/compat)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.metadata import installed_package_ids
from rwmod.mod_conflicts import find_mod_conflicts
from rwmod.mod_rules import (
    find_explicit_conflicts,
    find_rule_conflicts,
    load_user_rules,
    merge_rule_sources,
    normalize_package_id,
)
from rwmod.rimpy_db import RimPyDB

router = APIRouter(prefix="/api/compat", tags=["compat"])


@router.get("/check")
def compat_check(
    cfg: Config = Depends(get_config),
):
    """Check installed mods for missing dependencies and conflicts."""
    result = find_mod_conflicts(cfg.mods_dir)
    _add_rule_conflicts(result, cfg.mods_dir)

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


def _add_rule_conflicts(result: dict, mods_dir: Path) -> None:
    """Add user/community declared incompatibilities to a conflict result.

    Only *conflicts* are added — rule sources never invent missing
    dependencies, so a real install cannot gain false "missing" entries from
    the community database. The lookup is cache-only (no network).
    """
    installed = list(installed_package_ids(mods_dir))
    if not installed:
        return

    db = RimPyDB.get()
    db.load_cached()
    user_map, explicit = load_user_rules()
    merged = merge_rule_sources(user_map, dict(db.rules))

    found = find_rule_conflicts(merged, installed)
    found += find_explicit_conflicts(explicit, installed)
    if not found:
        return

    seen = {
        (
            min(normalize_package_id(c.get("a", "")), normalize_package_id(c.get("b", ""))),
            max(normalize_package_id(c.get("a", "")), normalize_package_id(c.get("b", ""))),
        )
        for c in result.get("conflicts", [])
    }
    for conflict in found:
        pair = (
            min(normalize_package_id(conflict["a"]), normalize_package_id(conflict["b"])),
            max(normalize_package_id(conflict["a"]), normalize_package_id(conflict["b"])),
        )
        if pair in seen:
            continue
        seen.add(pair)
        result.setdefault("conflicts", []).append(conflict)
    result["ok"] = not result.get("missing_dependencies") and not result.get("conflicts")
