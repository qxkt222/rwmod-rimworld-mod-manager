"""Mod compatibility analysis — dependency & conflict detection.

Scans every installed mod's About.xml for:
1. <modDependencies> — required mods that may be missing (or not enabled).
2. <incompatibleWith> — mods that must not be enabled together.

This complements load_order.py (which checks ordering rules) by providing
a per-mod dependency/conflict graph based on the mods' own metadata.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

_log = logging.getLogger(__name__)

__all__ = ["check_compatibility", "scan_mod_metadata"]


def scan_mod_metadata(mods_dir: Path) -> dict[str, dict]:
    """Scan all installed mods and extract dependency/conflict metadata.

    Returns {package_id: {"name": str, "dependencies": set[str],
    "incompatible": set[str]}}.
    """
    result: dict[str, dict] = {}
    for d in sorted(mods_dir.iterdir()):
        if not d.is_dir():
            continue
        about = d / "About" / "About.xml"
        if not about.exists():
            continue
        try:
            root = ET.parse(about).getroot()
        except Exception:
            continue
        pid = (root.findtext("packageId", "") or "").strip()
        if not pid:
            continue
        name = (root.findtext("name", "") or pid).strip()

        deps: set[str] = set()
        for el in root.findall("modDependencies"):
            for li in el.findall("li"):
                if li.text and li.text.strip():
                    deps.add(li.text.strip())

        incompatible: set[str] = set()
        for el in root.findall("incompatibleWith"):
            for li in el.findall("li"):
                if li.text and li.text.strip():
                    incompatible.add(li.text.strip())

        result[pid] = {
            "name": name,
            "dependencies": deps,
            "incompatible": incompatible,
        }
    return result


def check_compatibility(mods_dir: Path, active_ids: list[str] | None = None) -> dict:
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

    if active_ids is None:
        active_ids = list(metadata.keys())
    active_set = set(active_ids)

    # 1. Missing dependencies — a mod is active but one of its required deps
    #    is not installed at all.
    missing_dependencies: list[dict] = []
    for pid in active_ids:
        meta = metadata.get(pid)
        if not meta:
            continue
        missing = sorted(dep for dep in meta["dependencies"] if dep not in metadata)
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
        meta = metadata.get(pid)
        if not meta:
            continue
        for other in meta["incompatible"]:
            if other in active_set and other != pid:
                pair = tuple(sorted((pid, other)))
                if pair in seen:
                    continue
                seen.add(pair)
                other_name = metadata.get(other, {}).get("name", other)
                conflicts.append(
                    {
                        "a": pid,
                        "b": other,
                        "reason": f"「{meta['name']}」与「{other_name}」互不兼容，不能同时启用",
                    }
                )

    return {
        "missing_dependencies": missing_dependencies,
        "conflicts": conflicts,
        "ok": not missing_dependencies and not conflicts,
    }
