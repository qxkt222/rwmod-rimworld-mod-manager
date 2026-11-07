"""RimSort integration — generate ModsConfig.xml, compare, sync."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rwmod.metadata import (
    installed_mod_metas,
    installed_package_ids,
    package_id_to_workshop_id,
)
from rwmod.mod_rules import (
    CORE_PACKAGE_ID,
    OFFICIAL_PACKAGE_IDS,
    build_package_id_index,
    is_official_package_id,
    load_user_rules,
    merge_rule_sources,
    normalize_package_id,
    normalize_version,
    parse_dependency_groups,
    parse_package_id_list,
    parse_rule_document,
    select_versioned_dependencies,
    select_versioned_ids,
    split_anchor_order,
)
from rwmod.version_support import detect_rimworld_version
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


_OFFICIAL_NORMS: tuple[str, ...] = tuple(normalize_package_id(pid) for pid in OFFICIAL_PACKAGE_IDS)

# Data/<folder> → packageId fallback for installs whose About.xml is missing.
_OFFICIAL_FOLDER_PACKAGE_IDS = {pid.rsplit(".", 1)[-1]: pid for pid in OFFICIAL_PACKAGE_IDS}


def _game_version_string(rimworld_dir: Path | None) -> str:
    """Return the version string to write into ModsConfig.xml.

    RimWorld's own config uses the full Version.txt line ("1.6.4850 rev645"),
    not a synthetic "1.0.0". Returns "" when the install is unknown, in which
    case the element is omitted rather than filled with a placeholder.
    """
    if rimworld_dir is None:
        return ""
    version_file = Path(rimworld_dir) / "Version.txt"
    if version_file.exists():
        try:
            raw = version_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            raw = ""
        if raw:
            return raw.splitlines()[0].strip()
    return detect_rimworld_version(Path(rimworld_dir)) or ""


def _installed_official_ids(rimworld_dir: Path | None) -> list[str]:
    """Core + installed DLC packageIds, in canonical order (Data/ scan)."""
    if rimworld_dir is None:
        return []
    data_dir = Path(rimworld_dir) / "Data"
    if not data_dir.is_dir():
        return []

    found: dict[str, str] = {}
    for folder in sorted(data_dir.iterdir()):
        if not folder.is_dir():
            continue
        package_id = ""
        about = folder / "About" / "About.xml"
        if about.exists():
            try:
                package_id = (parse_xml_root(about).findtext("packageId") or "").strip()
            except Exception:
                package_id = ""
        if not package_id:
            package_id = _OFFICIAL_FOLDER_PACKAGE_IDS.get(folder.name.lower(), "")
        norm = normalize_package_id(package_id)
        if norm in _OFFICIAL_NORMS:
            found.setdefault(norm, package_id)
    return [found[norm] for norm in _OFFICIAL_NORMS if norm in found]


def generate_modsconfig(
    mods_dir: Path,
    output_path: Path | None = None,
    rimworld_dir: Path | None = None,
) -> str:
    """Generate a RimSort-compatible ModsConfig.xml from installed mods.

    Returns the XML string. If output_path is given, writes to disk.

    ``rimworld_dir`` supplies the real game version and installed DLC list;
    without it those elements are omitted instead of writing placeholders.
    """
    root = ET.Element("ModsConfigData")

    version = _game_version_string(rimworld_dir)
    if version:
        ET.SubElement(root, "version").text = version

    official_ids = _installed_official_ids(rimworld_dir)
    expansion_ids = [
        pid
        for pid in official_ids
        if normalize_package_id(pid) != normalize_package_id(CORE_PACKAGE_ID)
    ]
    if expansion_ids:
        known_expansions = ET.SubElement(root, "knownExpansions")
        for pid in expansion_ids:
            ET.SubElement(known_expansions, "li").text = pid

    active_mods = ET.SubElement(root, "activeMods")

    # installed_mod_metas yields directory-name order, which is the community
    # load order this generator has always emitted — do not rebuild it as a
    # set. Harmony/Core/DLC are anchored ahead of it.
    package_ids = [m.package_id for m in installed_mod_metas(mods_dir)]
    anchored, community = split_anchor_order([*package_ids, *official_ids])
    for pid in [*anchored, *community]:
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

    active_mods = config_data["active_mods"]
    config_norms = {normalize_package_id(pid) for pid in active_mods}
    installed_by_norm = build_package_id_index(installed_package_ids(mods_dir))

    # Core and every official DLC ship in the game's Data/ folder, not in
    # mods_dir, so they must never be reported as missing. Both the folder
    # name ("Core") and Core's real packageId are accepted (PARSE-7);
    # ``is_official_package_id`` covers the DLC packageIds as well — a real
    # 522-mod ModsConfig reported exactly five false "missing" entries, one
    # per installed DLC.
    core_norms = {normalize_package_id("Core"), normalize_package_id(CORE_PACKAGE_ID)}

    def _is_official(norm: str) -> bool:
        return norm in core_norms or is_official_package_id(norm)

    missing = [
        pid
        for pid in active_mods
        if normalize_package_id(pid) not in installed_by_norm
        and not _is_official(normalize_package_id(pid))
    ]
    # Official content is never "extra" either.
    extra = [
        pid
        for norm, pid in installed_by_norm.items()
        if norm not in config_norms and not _is_official(norm)
    ]
    installed_list = [pid for norm, pid in installed_by_norm.items() if norm in config_norms]

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

    # Build packageId → workshopId map from ALL About.xml files. ModsConfig
    # lowercases ids while About.xml keeps the author's casing, so the lookup
    # must be case-insensitive.
    wid_by_norm = {
        normalize_package_id(pkg): wid for pkg, wid in package_id_to_workshop_id(mods_dir).items()
    }

    results: list[dict] = []
    for pid in missing_package_ids:
        wid = wid_by_norm.get(normalize_package_id(pid), "")
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


def _collect_versioned_ids(root: ET.Element, tag: str) -> dict[str, set[str]]:
    """Read ``<loadAfterByVersion><v1.6><li>pid</li></v1.6>`` style rules."""
    collected: dict[str, set[str]] = {}
    for container in root.findall(tag):
        for version_el in list(container):
            version = normalize_version(version_el.tag)
            if not version:
                continue
            ids = set(parse_package_id_list(version_el))
            if ids:
                collected.setdefault(version, set()).update(ids)
    return collected


def _collect_versioned_dependencies(root: ET.Element, tag: str) -> dict[str, list[tuple[str, ...]]]:
    """Read ``<modDependenciesByVersion><v1.6><li>…</li></v1.6>`` groups."""
    collected: dict[str, list[tuple[str, ...]]] = {}
    for container in root.findall(tag):
        for version_el in list(container):
            version = normalize_version(version_el.tag)
            if not version:
                continue
            groups = parse_dependency_groups(version_el)
            for group in groups:
                bucket = collected.setdefault(version, [])
                if group not in bucket:
                    bucket.append(group)
    return collected


def _collect_load_rules(mods_dir: Path) -> dict[str, dict]:
    """Scan every installed mod and extract its load-order rules.

    Returns ``{package_id: rule}`` where ``rule`` carries the plain
    ``loadAfter`` / ``loadBefore`` / ``loadTop`` / ``loadBottom`` fields plus
    the 1.6 additions: ``forceLoadAfter`` / ``forceLoadBefore``,
    ``modDependencies`` OR-groups, ``loadAfterByVersion`` /
    ``modDependenciesByVersion`` version maps and ``incompatibleWith``.

    Rule owners keep their original spelling; every edge target is normalized
    (packageId comparisons are case-insensitive).

    A missing ``mods_dir`` yields ``{}`` instead of raising: callers treat
    "no mods" and "no mods directory" the same way, and this used to crash
    ``sort_mods`` / ``check_load_order`` with ``FileNotFoundError`` (PARSE-4).
    """
    if not mods_dir.is_dir():
        return {}
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
        force_after: set[str] = set()
        force_before: set[str] = set()
        incompatible: set[str] = set()
        dependency_groups: list[tuple[str, ...]] = []

        for el in root.findall("loadAfter"):
            load_after.update(parse_package_id_list(el))
        for el in root.findall("loadBefore"):
            load_before.update(parse_package_id_list(el))
        for el in root.findall("forceLoadAfter"):
            force_after.update(parse_package_id_list(el))
        for el in root.findall("forceLoadBefore"):
            force_before.update(parse_package_id_list(el))
        for el in root.findall("incompatibleWith"):
            incompatible.update(parse_package_id_list(el))
        for el in root.findall("modDependencies"):
            for group in parse_dependency_groups(el):
                if group not in dependency_groups:
                    dependency_groups.append(group)

        rules[pid] = {
            "loadAfter": load_after,
            "loadBefore": load_before,
            "forceLoadAfter": force_after,
            "forceLoadBefore": force_before,
            "loadTop": root.findtext("loadTop", "").strip().lower() == "true",
            "loadBottom": root.findtext("loadBottom", "").strip().lower() == "true",
            "dependencies": dependency_groups,
            "loadAfterByVersion": _collect_versioned_ids(root, "loadAfterByVersion"),
            "loadBeforeByVersion": _collect_versioned_ids(root, "loadBeforeByVersion"),
            "dependenciesByVersion": _collect_versioned_dependencies(
                root, "modDependenciesByVersion"
            ),
            "incompatibleWith": incompatible,
            "name": name,
        }
    return rules


def _detect_mods_version(mods_dir: Path) -> str:
    """Detect RimWorld's major.minor version next to ``mods_dir``.

    ``Mods/`` sits directly under the game root, so ``mods_dir.parent`` is the
    natural Version.txt location. Returns "" when no install can be found.
    """
    for candidate in (mods_dir.parent, mods_dir):
        try:
            version = detect_rimworld_version(candidate)
        except (OSError, UnicodeDecodeError):
            version = None
        if version:
            return version
    return ""


def _rule_edges(
    rules: Mapping[str, Mapping[str, Any]],
    active_ids: set[str],
    version: str | None = None,
) -> dict[str, set[str]]:
    """Build ``owner -> {ids that must load before owner}`` from merged rules.

    Includes loadAfter / loadBefore, forceLoadAfter / forceLoadBefore,
    modDependencies OR-groups and the version-specific rule groups selected by
    ``version`` (major.minor). Targets outside ``active_ids`` are ignored —
    they cannot be placed, and reporting them as unresolved would contradict
    the "active ids only" meaning of ``unresolved``.
    """
    active = {normalize_package_id(pid) for pid in active_ids}
    active.discard("")
    normalized = {
        normalize_package_id(owner): rule
        for owner, rule in rules.items()
        if normalize_package_id(owner)
    }

    def _after(rule: Mapping[str, Any]) -> set[str]:
        found = set(rule.get("loadAfter") or ())
        found.update(rule.get("forceLoadAfter") or ())
        found.update(select_versioned_ids(rule.get("loadAfterByVersion") or {}, version))
        return {normalize_package_id(x) for x in found if normalize_package_id(x) in active}

    def _before(rule: Mapping[str, Any]) -> set[str]:
        found = set(rule.get("loadBefore") or ())
        found.update(rule.get("forceLoadBefore") or ())
        found.update(select_versioned_ids(rule.get("loadBeforeByVersion") or {}, version))
        return {normalize_package_id(x) for x in found if normalize_package_id(x) in active}

    def _dependency_targets(rule: Mapping[str, Any]) -> set[str]:
        groups = list(rule.get("dependencies") or [])
        groups += select_versioned_dependencies(rule.get("dependenciesByVersion") or {}, version)
        targets: set[str] = set()
        for group in groups:
            # An OR-group is satisfied by any member; pin the first active one.
            for member in group:
                member_norm = normalize_package_id(str(member))
                if member_norm in active:
                    targets.add(member_norm)
                    break
        return targets

    edges: dict[str, set[str]] = {pid: set() for pid in active}
    for owner in active:
        rule = normalized.get(owner)
        if not isinstance(rule, Mapping):
            continue
        edges[owner].update(_after(rule))
        edges[owner].update(_dependency_targets(rule))
        for target in _before(rule):
            edges[target].add(owner)
    return edges


def _coerce_rule_source(
    value: Mapping[str, Any] | None, *, cache: bool
) -> tuple[dict[str, dict], list[dict]]:
    """Turn an injected rule document (or the default source) into rules.

    ``cache=True`` selects the community database; it is read from disk only
    (``load_cached``) so sorting never depends on the network. Explicit
    mappings — including a bare ``{packageId: rule}`` map — are accepted as-is.
    """
    if value is not None:
        return parse_rule_document(value)
    if cache:
        from rwmod.rimpy_db import RimPyDB

        db = RimPyDB.get()
        db.load_cached()
        return dict(db.rules), []
    return load_user_rules()


def _rule_source_kind(
    owner: str, dep: str, rules: Mapping[str, Mapping[str, Any]], version: str
) -> str:
    """Name the rule that produced an ``owner`` depends-on-``dep`` edge."""
    rule = rules.get(owner) or {}
    if dep in {normalize_package_id(x) for x in rule.get("forceLoadAfter") or ()}:
        return "forceLoadAfter"
    if dep in {normalize_package_id(x) for x in rule.get("loadAfter") or ()}:
        return "loadAfter"
    if dep in select_versioned_ids(rule.get("loadAfterByVersion") or {}, version):
        return "loadAfter"
    groups = list(rule.get("dependencies") or [])
    groups += select_versioned_dependencies(rule.get("dependenciesByVersion") or {}, version)
    for group in groups:
        if dep in {normalize_package_id(str(member)) for member in group}:
            return "dependency"
    other = rules.get(dep) or {}
    if owner in {normalize_package_id(x) for x in other.get("forceLoadBefore") or ()}:
        return "forceLoadBefore"
    return "loadBefore"


def sort_mods(
    mods_dir: Path,
    active_ids: list[str] | None = None,
    rimworld_version: str | None = None,
    *,
    community_rules: Mapping[str, Any] | None = None,
    user_rules: Mapping[str, Any] | None = None,
) -> dict:
    """Compute an optimal load order via topological sort.

    Args:
        mods_dir: RimWorld Mods directory.
        active_ids: Optional list of packageIds to sort. If None, all installed
            mods are sorted.
        rimworld_version: RimWorld version used to select the ``…ByVersion``
            rule groups. Defaults to Version.txt next to ``mods_dir``.
        community_rules: Community Rules document or bare rule map. Defaults
            to the cached community database (read from disk, never network).
        user_rules: User rules document or bare rule map. Defaults to
            ``~/.rwmod/rules.json`` (fail-open).

    Rule priority is user > community > built-in (About.xml). For one mod,
    the highest-priority source that declares a field wins.

    Returns:
        {
            "sorted": [package_id, ...],
            "unresolved": [package_id, ...],   # active ids not installed
            "cycles": [[package_id, ...], ...],# detected dependency cycles
            "violations": [                     # cross-partition edges that
                {"mod", "dependency", "kind", "reason"}  # loadTop/loadBottom wins over
            ],
            "rimworld_version": str,
        }
    """
    installed_rules = _collect_load_rules(mods_dir)

    ordered_ids = list(installed_rules) if active_ids is None else active_ids

    # Harmony -> Core -> DLC first. Anchored ids are removed from the
    # topological sort: the game pins them and Core/DLC have no About.xml in
    # mods_dir. split_anchor_order keeps the caller's spelling and drops
    # duplicates (after normalization).
    anchored, remaining_ids = split_anchor_order(ordered_ids)

    # Map every remaining active id through its normalized form. Active ids
    # with no installed rule cannot be sorted and are reported as unresolved
    # at the end instead of crashing the lookups. First occurrence wins if the
    # same mod appears twice with different casing.
    display: dict[str, str] = {}
    active_norms: list[str] = []
    for pid in remaining_ids:
        norm = normalize_package_id(pid)
        if norm and norm not in display:
            display[norm] = pid
            active_norms.append(norm)

    version = rimworld_version if rimworld_version is not None else _detect_mods_version(mods_dir)

    user_map, _user_conflicts = _coerce_rule_source(user_rules, cache=False)
    community_map, _community_conflicts = _coerce_rule_source(community_rules, cache=True)
    merged = merge_rule_sources(user_map, community_map, installed_rules)

    # Rules keyed by normalized id — ids we can actually place. The rest of
    # active_norms ends up unresolved.
    rules = {pid: merged[pid] for pid in active_norms if pid in merged}
    sortable_ids = list(rules)

    # Build adjacency: pid -> set of pids that must load BEFORE pid.
    depends_on = _rule_edges(rules, set(sortable_ids), version)
    depends_on = {pid: depends_on.get(pid, set()) for pid in sortable_ids}

    # loadTop mods have no dependencies on community mods (they go first).
    # loadBottom mods go last (nothing depends on them).
    top_ids = [pid for pid in sortable_ids if rules[pid]["loadTop"]]
    top_set = set(top_ids)
    # A mod declaring both wins "top": it must never be emitted twice.
    bottom_ids = [pid for pid in sortable_ids if rules[pid]["loadBottom"] and pid not in top_set]
    bottom_set = set(bottom_ids)
    middle_ids = [pid for pid in sortable_ids if pid not in top_set and pid not in bottom_set]

    def _name(pid: str) -> str:
        return str(rules[pid]["name"] or pid).lower()

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
        ready.sort(key=_name)
        queue: deque[str] = deque(ready)
        result: list[str] = []
        while queue:
            pid = queue.popleft()
            result.append(pid)
            for nxt in sorted(adj[pid], key=_name):
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
    sorted_norms = sorted_top + sorted_mid + sorted_bottom
    sorted_set = set(sorted_norms)
    unresolved_norms = [pid for pid in active_norms if pid not in sorted_set]
    cycles = [c for c in (cycles_top + cycles_mid + cycles_bottom) if c]

    # Cross-partition edges cannot be honoured (loadTop/loadBottom win), but
    # they must not vanish silently: report each one as a violation.
    tier = {pid: 0 for pid in top_ids}
    tier.update({pid: 1 for pid in middle_ids})
    tier.update({pid: 2 for pid in bottom_ids})
    violations: list[dict] = []
    seen_violations: set[tuple[str, str]] = set()
    for pid in sortable_ids:
        for dep in depends_on[pid]:
            if dep not in tier or tier[dep] <= tier[pid]:
                continue
            if (pid, dep) in seen_violations:
                continue
            seen_violations.add((pid, dep))
            violations.append(
                {
                    "mod": display[pid],
                    "dependency": display[dep],
                    "kind": _rule_source_kind(pid, dep, rules, version),
                    "reason": (
                        f"「{display[pid]}」声明需在「{display[dep]}」之后加载，"
                        "但 loadTop/loadBottom 分区锚定使该约束无法满足"
                    ),
                }
            )

    return {
        "sorted": [*anchored, *(display[pid] for pid in sorted_norms)],
        "unresolved": [display[pid] for pid in unresolved_norms],
        "cycles": [[display[pid] for pid in cycle] for cycle in cycles],
        "violations": violations,
        "rimworld_version": version,
    }
