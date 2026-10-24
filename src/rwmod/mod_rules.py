"""Shared packageId normalization and About.xml rule parsing.

RimWorld treats packageIds case-insensitively — it converts them to lowercase
internally — but About.xml files and ModsConfig.xml disagree on capitalization
in practice (a real ModsConfig may say ``brrainz.harmony`` while the About.xml
declares ``Brrainz.Harmony``). Any code that compares two packageIds must go
through :func:`normalize_package_id`; anything shown to the user or written
back to disk keeps its original spelling.

The module also owns the fixed official-content anchor order (Harmony → Core →
DLC) and the small parsers for the About.xml dependency lists, so sorting and
conflict detection agree on both.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

_log = logging.getLogger(__name__)

__all__ = [
    "ANCHOR_PACKAGE_IDS",
    "CORE_PACKAGE_ID",
    "HARMONY_PACKAGE_IDS",
    "OFFICIAL_PACKAGE_IDS",
    "build_package_id_index",
    "find_explicit_conflicts",
    "find_rule_conflicts",
    "is_official_package_id",
    "load_user_rules",
    "merge_rule_sources",
    "normalize_package_id",
    "normalize_version",
    "parse_dependency_groups",
    "parse_package_id_list",
    "parse_rule_document",
    "select_versioned_dependencies",
    "select_versioned_ids",
    "split_anchor_order",
]

#: Package ids in a rule document are always compared case-insensitively.
_VERSION_RE = re.compile(r"(\d+\.\d+)")

#: User rule files larger than this are rejected rather than parsed.
MAX_USER_RULES_BYTES = 2 * 1024 * 1024


def normalize_package_id(package_id: str | None) -> str:
    """Return the comparison form of a packageId (trimmed, lowercased)."""
    return (package_id or "").strip().lower()


#: Harmony — the mod-loader framework that must load before everything else.
HARMONY_PACKAGE_IDS: tuple[str, ...] = ("brrainz.harmony", "harmony")

#: Core's real packageId (the game ships it in Data/Core, not in Mods/).
CORE_PACKAGE_ID = "ludeon.rimworld"

#: Core + official DLCs in canonical load order (DLC release order).
OFFICIAL_PACKAGE_IDS: tuple[str, ...] = (
    "ludeon.rimworld",
    "ludeon.rimworld.royalty",
    "ludeon.rimworld.ideology",
    "ludeon.rimworld.biotech",
    "ludeon.rimworld.anomaly",
    "ludeon.rimworld.odyssey",
)

#: Fixed prefix for every generated/sorted load order.
ANCHOR_PACKAGE_IDS: tuple[str, ...] = (*HARMONY_PACKAGE_IDS, *OFFICIAL_PACKAGE_IDS)

_OFFICIAL_NORMS = frozenset(normalize_package_id(pid) for pid in OFFICIAL_PACKAGE_IDS)
_ANCHOR_NORMS = tuple(normalize_package_id(pid) for pid in ANCHOR_PACKAGE_IDS)


def build_package_id_index(package_ids: Iterable[str]) -> dict[str, str]:
    """Map ``normalize_package_id(pid) -> first original spelling``.

    Empty ids are skipped: they cannot identify a mod and must never become a
    lookup key (an empty ModsConfig entry should not match an empty packageId).
    """
    index: dict[str, str] = {}
    for package_id in package_ids:
        key = normalize_package_id(package_id)
        if key and key not in index:
            index[key] = package_id
    return index


def is_official_package_id(package_id: str | None) -> bool:
    """True for Core and every DLC packageId.

    The game installs these under ``Data/`` rather than ``Mods/``, so metadata
    scans never see them. Any ``ludeon.rimworld.*`` id counts as official so a
    future DLC does not turn into a wave of false "missing dependency" reports.
    """
    norm = normalize_package_id(package_id)
    return norm in _OFFICIAL_NORMS or norm.startswith(CORE_PACKAGE_ID + ".")


def split_anchor_order(package_ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split ids into ``(anchored, rest)``.

    ``anchored`` contains Harmony/Core/DLC in :data:`ANCHOR_PACKAGE_IDS` order,
    using the caller's original spelling. ``rest`` keeps the caller's order.
    Duplicates (after normalization) are dropped from both lists.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for package_id in package_ids:
        norm = normalize_package_id(package_id)
        if norm and norm not in seen:
            seen.add(norm)
            ordered.append(package_id)

    by_norm = {normalize_package_id(pid): pid for pid in ordered}
    anchored = [by_norm[norm] for norm in _ANCHOR_NORMS if norm in by_norm]
    anchor_set = set(_ANCHOR_NORMS)
    rest = [pid for pid in ordered if normalize_package_id(pid) not in anchor_set]
    return anchored, rest


def parse_package_id_list(element: Element | None) -> list[str]:
    """Read a list element into normalized packageIds.

    Accepts both the flat form ``<li>author.mod</li>`` and the 1.4+ rich form
    ``<li><packageId>author.mod</packageId>…</li>``. Empty entries, duplicate
    ids and unknown child elements are ignored. ``IgnoreIfNoMatchingField`` is
    an XML attribute and needs no special handling.
    """
    if element is None:
        return []
    ids: list[str] = []
    for li in element.findall("li"):
        text = (li.text or "").strip()
        if text:
            ids.append(normalize_package_id(text))
            continue
        package_id = (li.findtext("packageId") or "").strip()
        if package_id:
            ids.append(normalize_package_id(package_id))
    return list(dict.fromkeys(ids))


def parse_dependency_groups(element: Element | None) -> list[tuple[str, ...]]:
    """Read ``<modDependencies>`` into OR-groups of normalized packageIds.

    Each group is ``(primary, *alternativePackageIds)``: the dependency is
    satisfied when *any* member is installed. Flat ``<li>pid</li>`` entries
    produce a one-element group.
    """
    if element is None:
        return []
    groups: list[tuple[str, ...]] = []
    for li in element.findall("li"):
        text = (li.text or "").strip()
        group: tuple[str, ...]
        if text:
            group = (normalize_package_id(text),)
        else:
            primary = (li.findtext("packageId") or "").strip()
            alternatives = [
                (alt.text or "").strip() for alt in li.findall("alternativePackageIds/li")
            ]
            members = [pid for pid in (primary, *alternatives) if pid]
            group = tuple(dict.fromkeys(normalize_package_id(pid) for pid in members))
        if group and group not in groups:
            groups.append(group)
    return groups


# ── rule documents (community rules / user rules) ──────────────────
#
# Rule documents share one shape with the RimSort Community Rules Database:
#
#   {"timestamp": int,
#    "rules": {"<packageId>": {
#        "loadAfter":        {"<pid>": {"name": [...], "comment": str}},
#        "loadBefore":       {"<pid>": {...}},
#        "loadTop":          {"value": true, "comment": str},
#        "loadBottom":       {"value": true, "comment": str},
#        "incompatibleWith": {"<pid>": {...}}}}}
#
# The dead RimPy database ({"conflicts": [...], "load_rules": [...]}) is
# accepted too and migrated on read. Parsed rule mappings are *sparse*:
# a field is present only when the document declared it, which is what lets
# :func:`merge_rule_sources` implement "user > community > built-in" as a
# per-owner, per-field override instead of a blind union.

_SET_FIELDS = ("loadAfter", "loadBefore", "forceLoadAfter", "forceLoadBefore", "incompatibleWith")
_BOOL_FIELDS = ("loadTop", "loadBottom")
_LIST_FIELDS = ("dependencies",)
_VERSION_SET_FIELDS = ("loadAfterByVersion", "loadBeforeByVersion")
_VERSION_LIST_FIELDS = ("dependenciesByVersion",)


def normalize_version(value: str | None) -> str:
    """Return major.minor from a version string ("1.6.4850 rev645" -> "1.6")."""
    match = _VERSION_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, Mapping):
        value = value.get("value", False)
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _target_ids(value: Any) -> set[str]:
    """Read a rule target collection into normalized packageIds.

    Accepts the community mapping form ``{"pid": metadata}``, a plain list,
    a single string, or a list of rich ``{"packageId": ...}`` entries.
    """
    if isinstance(value, str):
        norm = normalize_package_id(value)
        return {norm} if norm else set()
    if isinstance(value, Mapping):
        return {norm for key, _ in value.items() if (norm := normalize_package_id(key))}
    if isinstance(value, Iterable):
        found: set[str] = set()
        for item in value:
            if isinstance(item, str):
                norm = normalize_package_id(item)
            elif isinstance(item, Mapping):
                norm = normalize_package_id(
                    str(item.get("packageId") or item.get("id") or item.get("name") or "")
                )
            else:
                norm = ""
            if norm:
                found.add(norm)
        return found
    return set()


def _dependency_groups(value: Any) -> list[tuple[str, ...]]:
    """Read JSON dependency declarations into OR-groups."""
    if isinstance(value, Mapping):
        value = [{"packageId": key} for key in value]
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return []
    groups: list[tuple[str, ...]] = []
    for item in value:
        group: tuple[str, ...]
        if isinstance(item, str):
            group = (normalize_package_id(item),)
        elif isinstance(item, Mapping):
            primary = str(item.get("packageId") or item.get("id") or "")
            alternatives = item.get("alternativePackageIds") or []
            members = [primary, *(str(alt) for alt in alternatives)]
            group = tuple(
                dict.fromkeys(norm for pid in members if (norm := normalize_package_id(pid)))
            )
        elif isinstance(item, (list, tuple)):
            # Already-parsed group (e.g. when a parsed rule map is re-injected).
            group = tuple(
                dict.fromkeys(norm for pid in item if (norm := normalize_package_id(str(pid))))
            )
        else:
            group = ()
        if group and group not in groups:
            groups.append(group)
    return groups


def _versioned_ids(value: Any) -> dict[str, set[str]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _target_ids(items)
        for key, items in value.items()
        if isinstance(key, str) and _target_ids(items)
    }


def _versioned_dependencies(value: Any) -> dict[str, list[tuple[str, ...]]]:
    if not isinstance(value, Mapping):
        return {}
    parsed: dict[str, list[tuple[str, ...]]] = {}
    for key, items in value.items():
        groups = _dependency_groups(items)
        if isinstance(key, str) and groups:
            parsed[str(key)] = groups
    return parsed


def _parse_rule_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    name = str(spec.get("name") or "").strip()
    if name:
        rule["name"] = name
    for field in _SET_FIELDS:
        if field in spec:
            rule[field] = _target_ids(spec[field])
    for field in _BOOL_FIELDS:
        if field in spec:
            rule[field] = _as_bool(spec[field])
    for source_key, field in (
        ("dependencies", "dependencies"),
        ("modDependencies", "dependencies"),
    ):
        if source_key in spec and field not in rule:
            rule[field] = _dependency_groups(spec[source_key])
    if "loadAfterByVersion" in spec:
        rule["loadAfterByVersion"] = _versioned_ids(spec["loadAfterByVersion"])
    if "loadBeforeByVersion" in spec:
        rule["loadBeforeByVersion"] = _versioned_ids(spec["loadBeforeByVersion"])
    for source_key in ("dependenciesByVersion", "modDependenciesByVersion"):
        if source_key in spec and "dependenciesByVersion" not in rule:
            rule["dependenciesByVersion"] = _versioned_dependencies(spec[source_key])
    return rule


def _parse_rule_map(raw: Mapping[Any, Any]) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for owner, spec in raw.items():
        owner_norm = normalize_package_id(owner if isinstance(owner, str) else "")
        if not owner_norm or not isinstance(spec, Mapping):
            continue
        parsed = _parse_rule_spec(spec)
        if parsed and owner_norm not in rules:
            rules[owner_norm] = parsed
    return rules


def _parse_conflicts(value: Any) -> list[dict]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return []
    conflicts: list[dict] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        a = normalize_package_id(str(item.get("a") or item.get("mod_a") or item.get("id1") or ""))
        b = normalize_package_id(str(item.get("b") or item.get("mod_b") or item.get("id2") or ""))
        if not a or not b or a == b:
            continue
        reason = str(
            item.get("reason") or item.get("description") or item.get("comment") or ""
        ).strip()
        conflicts.append({"a": a, "b": b, "reason": reason})
    return conflicts


def _migrate_legacy_document(
    data: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict]]:
    """Convert the dead RimPy schema into the community-rules rule shape."""
    rules: dict[str, dict[str, Any]] = {}
    for raw in data.get("load_rules") or []:
        if not isinstance(raw, Mapping):
            continue
        owner = normalize_package_id(str(raw.get("id") or raw.get("mod_id") or ""))
        if not owner:
            continue
        rule = rules.setdefault(owner, {})
        after = _target_ids(raw.get("after"))
        before = _target_ids(raw.get("before"))
        if after:
            rule["loadAfter"] = after
        if before:
            rule["loadBefore"] = before
    return rules, _parse_conflicts(data.get("conflicts"))


def parse_rule_document(data: object) -> tuple[dict[str, dict[str, Any]], list[dict]]:
    """Parse a community/user rule document.

    Returns ``(rules, conflicts)`` where ``rules`` maps a normalized packageId
    to a sparse rule dict and ``conflicts`` is a list of explicit
    ``{"a", "b", "reason"}`` pairs. Unknown or malformed entries are ignored —
    rule sources are advisory and must never make sorting fail.
    """
    if not isinstance(data, Mapping):
        return {}, []
    if isinstance(data.get("rules"), Mapping):
        return _parse_rule_map(data["rules"]), _parse_conflicts(data.get("conflicts"))
    if any(key in data for key in ("load_rules", "mods", "timestamp")):
        return _migrate_legacy_document(data)
    # A bare {packageId: rule} map; explicit conflicts may still accompany it.
    return _parse_rule_map(data), _parse_conflicts(data.get("conflicts"))


def merge_rule_sources(
    *sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge rule sources ordered from highest to lowest priority.

    The per-owner, per-field rule is: the first source that *declares* a field
    wins. This is what implements ``user > community > built-in`` without
    turning every declaration into a union (a user who overrides a mod's
    ``loadAfter`` replaces the community list for that mod, not appends to it).
    The merged result is dense: every canonical field is present.
    """
    normalized: list[dict[str, Mapping[str, Any]]] = []
    owners: list[str] = []
    seen: set[str] = set()
    for source in sources:
        current: dict[str, Mapping[str, Any]] = {}
        for owner, spec in source.items():
            norm = normalize_package_id(owner)
            if not norm or not isinstance(spec, Mapping) or norm in current:
                continue
            current[norm] = spec
            if norm not in seen:
                seen.add(norm)
                owners.append(norm)
        normalized.append(current)

    merged: dict[str, dict[str, Any]] = {}
    for owner in owners:
        rules = [source.get(owner, {}) for source in normalized]
        out: dict[str, Any] = {
            "name": "",
            "loadAfter": set(),
            "loadBefore": set(),
            "forceLoadAfter": set(),
            "forceLoadBefore": set(),
            "loadTop": False,
            "loadBottom": False,
            "dependencies": [],
            "loadAfterByVersion": {},
            "loadBeforeByVersion": {},
            "dependenciesByVersion": {},
            "incompatibleWith": set(),
        }
        for rule in rules:
            name = str(rule.get("name") or "").strip()
            if name:
                out["name"] = name
                break
        for field in _SET_FIELDS:
            out[field] = set()
            for rule in rules:
                if field in rule:
                    out[field] = {normalize_package_id(x) for x in rule.get(field) or ()}
                    out[field].discard("")
                    break
        for field in _BOOL_FIELDS:
            out[field] = False
            for rule in rules:
                if field in rule:
                    out[field] = bool(rule.get(field))
                    break
        for field in _LIST_FIELDS:
            out[field] = []
            for rule in rules:
                if field in rule:
                    out[field] = list(rule.get(field) or [])
                    break
        for field in _VERSION_SET_FIELDS:
            out[field] = {}
            for rule in rules:
                if field in rule:
                    out[field] = dict(rule.get(field) or {})
                    break
        for field in _VERSION_LIST_FIELDS:
            out[field] = {}
            for rule in rules:
                if field in rule:
                    out[field] = dict(rule.get(field) or {})
                    break
        merged[owner] = out
    return merged


def select_versioned_ids(mapping: Mapping[str, Iterable[str]], version: str | None) -> set[str]:
    """Return the ids declared for ``version`` (major.minor) in a version map."""
    target = normalize_version(version)
    if not target:
        return set()
    found: set[str] = set()
    for key, ids in mapping.items():
        if normalize_version(key) == target:
            found.update(normalize_package_id(pid) for pid in ids)
    found.discard("")
    return found


def select_versioned_dependencies(
    mapping: Mapping[str, Sequence[Sequence[str]]], version: str | None
) -> list[tuple[str, ...]]:
    """Return the dependency groups declared for ``version``."""
    target = normalize_version(version)
    if not target:
        return []
    groups: list[tuple[str, ...]] = []
    for key, items in mapping.items():
        if normalize_version(key) != target:
            continue
        for group in items:
            normalized = tuple(norm for pid in group if (norm := normalize_package_id(str(pid))))
            if normalized and normalized not in groups:
                groups.append(normalized)
    return groups


def find_rule_conflicts(
    rules: Mapping[str, Mapping[str, Any]], active_ids: Iterable[str]
) -> list[dict]:
    """Return declared incompatibilities between active packageIds."""
    active = build_package_id_index(active_ids)
    active_norms = set(active)
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for owner in active_norms:
        rule = rules.get(owner)
        if not isinstance(rule, Mapping):
            continue
        for target in rule.get("incompatibleWith") or ():
            target_norm = normalize_package_id(str(target))
            if not target_norm or target_norm == owner or target_norm not in active_norms:
                continue
            pair = (min(owner, target_norm), max(owner, target_norm))
            if pair in seen:
                continue
            seen.add(pair)
            found.append(
                {
                    "a": active[pair[0]],
                    "b": active[pair[1]],
                    "reason": "被规则标记为不兼容，不能同时启用",
                }
            )
    return found


def find_explicit_conflicts(
    conflicts: Iterable[Mapping[str, Any]], active_ids: Iterable[str]
) -> list[dict]:
    """Filter explicit user-declared conflict pairs to the active set."""
    active = build_package_id_index(active_ids)
    active_norms = set(active)
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for conflict in conflicts:
        a = normalize_package_id(str(conflict.get("a") or ""))
        b = normalize_package_id(str(conflict.get("b") or ""))
        if not a or not b or a == b or a not in active_norms or b not in active_norms:
            continue
        pair = (min(a, b), max(a, b))
        if pair in seen:
            continue
        seen.add(pair)
        found.append(
            {
                "a": active[pair[0]],
                "b": active[pair[1]],
                "reason": str(conflict.get("reason") or "用户规则标记为冲突").strip(),
            }
        )
    return found


def load_user_rules(
    path: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict]]:
    """Load ``~/.rwmod/rules.json`` (or an explicit path), fail-open.

    A missing, oversized, unreadable or malformed file yields empty rules:
    the file is an optional local override and must never break sorting or
    diagnostics while offline.
    """
    rules_path = Path(path) if path is not None else Path.home() / ".rwmod" / "rules.json"
    try:
        if not rules_path.is_file():
            return {}, []
        if rules_path.stat().st_size > MAX_USER_RULES_BYTES:
            _log.warning("Ignoring oversized user rules file: %s", rules_path)
            return {}, []
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.debug("Could not read user rules %s: %s", rules_path, exc)
        return {}, []
    return parse_rule_document(data)
