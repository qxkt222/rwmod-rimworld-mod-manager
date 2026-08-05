"""RimSort integration — generate ModsConfig.xml, compare, sync."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from rwmod.xmlutil import parse_xml_root

# Safe XML parser: rejects entity-expansion / external-entity (XXE) attacks.
# NOTE: defusedxml.ElementTree only provides *parsing* functions (parse,
# fromstring, iterparse) — it deliberately does NOT expose Element/SubElement
# builders. So we use defusedxml for parsing untrusted About.xml/ModsConfig.xml
# and stdlib ET for *building* new XML documents (generate_modsconfig).

__all__ = [
    "generate_modsconfig",
    "compare_modsconfig",
    "parse_modsconfig",
    "resolve_missing_workshop_ids",
    "sort_mods",
]


def generate_modsconfig(mods_dir: Path, output_path: Path | None = None) -> str:
    """Generate a RimSort-compatible ModsConfig.xml from installed mods.

    Returns the XML string. If output_path is given, writes to disk.
    """
    root = ET.Element("ModsConfigData")

    ET.SubElement(root, "version").text = "1.0.0"
    ET.SubElement(root, "buildNumber").text = "0"

    active_mods = ET.SubElement(root, "activeMods")

    # Collect packageIds from installed mods
    package_ids: list[str] = []
    for d in sorted(mods_dir.iterdir()):
        if not d.is_dir():
            continue
        about = d / "About" / "About.xml"
        if about.exists():
            try:
                pid = parse_xml_root(about).findtext("packageId", "")
                if pid:
                    package_ids.append(pid)
            except Exception:
                pass

    for pid in package_ids:
        ET.SubElement(active_mods, "li").text = pid

    et = ET.ElementTree(root)
    ET.indent(et, space="  ")
    xml_str = ET.tostring(root, encoding="unicode")

    if output_path:
        output_path.write_text(xml_str, encoding="utf-8")

    return xml_str


def parse_modsconfig(path: Path) -> dict:
    """Parse a ModsConfig.xml and return its structure."""
    if not path.exists():
        return {"error": "file not found"}

    try:
        root = parse_xml_root(path)
    except Exception as e:
        return {"error": str(e)}

    active_mods = root.find("activeMods")
    items: list[str] = []
    if active_mods is not None:
        for li in active_mods.findall("li"):
            if li.text:
                items.append(li.text.strip())

    return {
        "version": root.findtext("version", ""),
        "build_number": root.findtext("buildNumber", ""),
        "active_mods": items,
        "total": len(items),
    }


def compare_modsconfig(modsconfig_path: Path, mods_dir: Path) -> dict:
    """Compare ModsConfig.xml with installed mods.

    Returns: { installed: [...], missing: [...], extra: [...], load_order: [...] }
    """
    config_data = parse_modsconfig(modsconfig_path)
    if "error" in config_data:
        return config_data

    config_ids = set(config_data["active_mods"])
    installed_ids: set[str] = set()

    for d in mods_dir.iterdir():
        about = d / "About" / "About.xml"
        if about.exists():
            try:
                pid = parse_xml_root(about).findtext("packageId", "")
                if pid:
                    installed_ids.add(pid)
            except Exception:
                pass

    # Core is RimWorld's built-in mod — it lives in the game's Data/Core
    # folder, not in mods_dir, so it must never be reported as missing.
    config_ids.discard("Core")
    installed_ids.add("Core")

    missing = [pid for pid in config_data["active_mods"] if pid not in installed_ids]
    # Core is RimWorld's built-in mod — never report it as "extra" either.
    extra = list((installed_ids - config_ids) - {"Core"})
    installed_list = list(installed_ids & config_ids)

    return {
        "load_order": config_data["active_mods"],
        "total_in_config": len(config_data["active_mods"]),
        "installed": installed_list,
        "installed_count": len(installed_list),
        "missing": missing,
        "missing_count": len(missing),
        "extra": extra,
        "extra_count": len(extra),
    }


def resolve_missing_workshop_ids(missing_package_ids: list[str], mods_dir: Path) -> list[dict]:
    """For missing packageIds, try to resolve to workshop IDs from installed mods.

    This is a best-effort lookup — it works if the mod was previously installed
    and its PublishedFileId.txt exists elsewhere.
    """

    # Build packageId → workshopId map from ALL About.xml files
    pkg_to_wid: dict[str, str] = {}
    for d in mods_dir.iterdir():
        pf = d / "About" / "PublishedFileId.txt"
        about = d / "About" / "About.xml"
        if pf.exists() and about.exists():
            try:
                pid = parse_xml_root(about).findtext("packageId", "")
                wid = pf.read_text(encoding="utf-8").strip()
                if pid and wid:
                    pkg_to_wid[pid] = wid
            except Exception:
                pass

    results: list[dict] = []
    for pid in missing_package_ids:
        wid = pkg_to_wid.get(pid, "")
        results.append({"package_id": pid, "workshop_id": wid, "resolved": bool(wid)})
    return results


# ── automatic load-order sorting ───────────────────────────────────


def _read_about(mod_dir: Path) -> ET.Element | None:
    """Parse a mod's About.xml, returning the root element or None.

    Uses defusedxml (safe_parse) so a maliciously-crafted About.xml with
    entity-expansion / external entities is rejected rather than exploited.
    """
    about = mod_dir / "About" / "About.xml"
    if not about.exists():
        return None
    try:
        return parse_xml_root(about)
    except Exception:
        return None


def _collect_load_rules(mods_dir: Path) -> dict[str, dict]:
    """Scan every installed mod and extract its load-order rules.

    Returns {package_id: {"loadAfter": set, "loadBefore": set, "loadTop": bool,
    "loadBottom": bool, "name": str}}.
    """
    rules: dict[str, dict] = {}
    for d in sorted(mods_dir.iterdir()):
        if not d.is_dir():
            continue
        root = _read_about(d)
        if root is None:
            continue
        pid = (root.findtext("packageId", "") or "").strip()
        if not pid:
            continue
        name = (root.findtext("name", "") or pid).strip()

        load_after: set[str] = set()
        load_before: set[str] = set()
        load_top = False
        load_bottom = False

        for el in root.findall("loadAfter"):
            for li in el.findall("li"):
                if li.text and li.text.strip():
                    load_after.add(li.text.strip())
        for el in root.findall("loadBefore"):
            for li in el.findall("li"):
                if li.text and li.text.strip():
                    load_before.add(li.text.strip())
        if root.findtext("loadTop", "").strip().lower() == "true":
            load_top = True
        if root.findtext("loadBottom", "").strip().lower() == "true":
            load_bottom = True

        rules[pid] = {
            "loadAfter": load_after,
            "loadBefore": load_before,
            "loadTop": load_top,
            "loadBottom": load_bottom,
            "name": name,
        }
    return rules


def sort_mods(mods_dir: Path, active_ids: list[str] | None = None) -> dict:
    """Compute an optimal load order via topological sort.

    Args:
        mods_dir: RimWorld Mods directory.
        active_ids: Optional list of packageIds to sort. If None, all installed
            mods are sorted.

    Returns:
        {
            "sorted": [package_id, ...],
            "unresolved": [package_id, ...],   # mods with cyclic/unknown deps
            "cycles": [[package_id, ...], ...],# detected dependency cycles
        }
    """
    rules = _collect_load_rules(mods_dir)

    if active_ids is None:
        active_ids = list(rules.keys())
    else:
        # Only keep rules for mods that are actually active
        rules = {pid: r for pid, r in rules.items() if pid in active_ids}

    # Build adjacency: pid -> set of pids that must load BEFORE pid
    # (i.e. pid depends on them). loadAfter adds an edge dep -> pid.
    # loadBefore on pid means pid must load before target, so target -> pid edge.
    depends_on: dict[str, set[str]] = {pid: set() for pid in active_ids}
    for pid, r in rules.items():
        for dep in r["loadAfter"]:
            if dep in rules:
                depends_on[pid].add(dep)
        for target in r["loadBefore"]:
            if target in rules:
                depends_on[target].add(pid)

    # loadTop mods have no dependencies on community mods (they go first).
    # loadBottom mods go last (nothing depends on them).
    top_ids = [pid for pid in active_ids if rules[pid]["loadTop"]]
    bottom_ids = [pid for pid in active_ids if rules[pid]["loadBottom"]]
    middle_ids = [pid for pid in active_ids if pid not in top_ids and pid not in bottom_ids]

    def _topo(nodes: list[str]) -> tuple[list[str], list[list[str]]]:
        """Kahn's algorithm. Returns (sorted, cycles).

        Uses a deque for O(1) popleft instead of list.pop(0) which is O(n) —
        important for load orders with thousands of mods.
        """
        indegree = {pid: 0 for pid in nodes}
        adj: dict[str, list[str]] = {pid: [] for pid in nodes}
        for pid in nodes:
            for dep in depends_on[pid]:
                if dep in nodes:
                    adj[dep].append(pid)
                    indegree[pid] += 1
        ready = [pid for pid in nodes if indegree[pid] == 0]
        # Stable order: sort by name for deterministic output
        ready.sort(key=lambda p: rules[p]["name"].lower())
        queue: deque[str] = deque(ready)
        result: list[str] = []
        while queue:
            pid = queue.popleft()
            result.append(pid)
            for nxt in sorted(adj[pid], key=lambda p: rules[p]["name"].lower()):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        cycles: list[list[str]] = []
        remaining = [pid for pid in nodes if pid not in result]
        if remaining:
            cycles.append(remaining)
        return result, cycles

    sorted_top, cycles_top = _topo(top_ids)
    sorted_mid, cycles_mid = _topo(middle_ids)
    sorted_bottom, cycles_bottom = _topo(bottom_ids)

    # Merge: top + middle + bottom, then unresolved (cyclic) at the end.
    sorted_ids = sorted_top + sorted_mid + sorted_bottom
    unresolved = [pid for pid in active_ids if pid not in sorted_ids]
    cycles = [c for c in (cycles_top + cycles_mid + cycles_bottom) if c]

    return {"sorted": sorted_ids, "unresolved": unresolved, "cycles": cycles}
