"""Mod conflict analysis — missing dependencies & declared incompatibilities.

Scans every installed mod's About.xml for:
1. <modDependencies> — required mods that may be missing (or not enabled).
2. <incompatibleWith> — mods that must not be enabled together.

This is the *mod-level* view. For "does this mod support my RimWorld version",
see version_support.py. The two modules were called compat.py and
compatibility.py, whose names differed by six characters while exporting the
same function name — renamed so call sites are readable on their own.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rwmod.mod_rules import (
    is_official_package_id,
    normalize_package_id,
    parse_dependency_groups,
    parse_package_id_list,
)
from rwmod.xmlutil import parse_xml_root

# Safe XML parser: rejects entity-expansion / external-entity (XXE) attacks.

_log = logging.getLogger(__name__)

__all__ = ["find_mod_conflicts", "scan_mod_metadata"]


def scan_mod_metadata(mods_dir: Path) -> dict[str, dict]:
    """Scan all installed mods and extract dependency/conflict metadata.

    Returns ``{package_id: {"name": str, "dependencies": set[str],
    "dependency_groups": list[tuple[str, ...]], "incompatible": set[str]}}``.

    ``dependencies`` holds the primary packageId of every requirement (kept
    for backwards compatibility). ``dependency_groups`` preserves the OR
    semantics of 1.4+ rich entries: an item is ``(primary, *alternatives)``
    and is satisfied when any member is installed.

    A missing ``mods_dir`` yields ``{}`` instead of raising, mirroring
    ``metadata.installed_mod_metas``; ``/api/compat/check`` used to answer 500
    on an unset/absent Mods directory (PARSE-4).
    """
    if not mods_dir.is_dir():
        return {}
    result: dict[str, dict] = {}
    for d in sorted(mods_dir.iterdir()):
        if not d.is_dir():
            continue
        about = d / "About" / "About.xml"
        if not about.exists():
            continue
        try:
            root = parse_xml_root(about)
        except Exception:
            continue
        pid = (root.findtext("packageId", "") or "").strip()
        if not pid:
            continue
        name = (root.findtext("name", "") or pid).strip()

        # 1.4+ About.xml uses <li><packageId>…</packageId></li> for every
        # dependency; reading only li.text (the pre-1.4 flat form) saw none of
        # the 378 real mods that declare dependencies.
        groups: list[tuple[str, ...]] = []
        for el in root.findall("modDependencies"):
            for group in parse_dependency_groups(el):
                if group not in groups:
                    groups.append(group)

        incompatible: set[str] = set()
        for el in root.findall("incompatibleWith"):
            incompatible.update(parse_package_id_list(el))

        result[pid] = {
            "name": name,
            "dependencies": {group[0] for group in groups},
            "dependency_groups": groups,
            "incompatible": incompatible,
        }
    return result


def find_mod_conflicts(mods_dir: Path, active_ids: list[str] | None = None) -> dict:
    """Analyze installed mods for missing dependencies and conflicts.

    Args:
        mods_dir: RimWorld Mods directory.
        active_ids: Optional list of enabled packageIds. If None, all installed
            mods are considered active.

    Returns:
        {
            "missing_dependencies": [{"mod": str, "name": str, "missing": [str]}],
            "conflicts": [{"a": str, "b": str, "reason": str}],
            "ok": bool,
        }
    """
    metadata = scan_mod_metadata(mods_dir)

    # packageIds are case-insensitive in RimWorld, but ModsConfig.xml and
    # About.xml rarely agree on capitalization. Compare through the normalized
    # form; keep the original spelling for every field we return.
    installed_by_norm: dict[str, tuple[str, dict]] = {}
    for pid, meta in metadata.items():
        installed_by_norm.setdefault(normalize_package_id(pid), (pid, meta))

    if active_ids is None:
        active_ids = list(metadata.keys())
    active_norms = {normalize_package_id(pid) for pid in active_ids}

    # 1. Missing dependencies — a mod is active but one of its required deps
    #    is not installed at all.
    missing_dependencies: list[dict] = []
    seen_active: set[str] = set()
    for pid in active_ids:
        norm = normalize_package_id(pid)
        if norm in seen_active:
            continue
        seen_active.add(norm)
        entry = installed_by_norm.get(norm)
        if entry is None:
            continue
        _, meta = entry
        # A rich dependency is satisfied by ANY of its alternativePackageIds.
        # Core/DLC ship in Data/, never in mods_dir, so they are treated as
        # installed — otherwise 72 real references would be false positives.
        groups = meta.get("dependency_groups") or [(dep,) for dep in meta["dependencies"]]
        missing = sorted(
            {
                group[0]
                for group in groups
                if not any(alt in installed_by_norm or is_official_package_id(alt) for alt in group)
            }
        )
        if missing:
            missing_dependencies.append(
                {
                    "mod": pid,
                    "name": meta["name"],
                    "missing": missing,
                }
            )

    # 2. Conflicts — two active mods declare each other (or one declares the
    #    other) as incompatible.
    conflicts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for pid in active_ids:
        pid_norm = normalize_package_id(pid)
        entry = installed_by_norm.get(pid_norm)
        if entry is None:
            continue
        _, meta = entry
        for other in meta["incompatible"]:
            other_norm = normalize_package_id(other)
            if other_norm in active_norms and other_norm != pid_norm:
                pair: tuple[str, str] = (min(pid_norm, other_norm), max(pid_norm, other_norm))
                if pair in seen:
                    continue
                seen.add(pair)
                other_entry = installed_by_norm.get(other_norm)
                other_pid = other_entry[0] if other_entry else other
                other_name = other_entry[1].get("name", other_pid) if other_entry else other
                conflicts.append(
                    {
                        "a": pid,
                        "b": other_pid,
                        "reason": f"「{meta['name']}」与「{other_name}」互不兼容，不能同时启用",
                    }
                )

    return {
        "missing_dependencies": missing_dependencies,
        "conflicts": conflicts,
        "ok": not missing_dependencies and not conflicts,
    }
