"""Load order analysis — detect common RimWorld mod ordering issues.

Checks the active ModsConfig.xml for:
1. Harmony position (must load first)
2. Core + DLC ordering
3. Known incompatible mod pairs
4. Mods with missing dependencies in the load order
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rwmod.errors import ModNotFoundError, RwmodError, ValidationError
from rwmod.metadata import package_id_to_workshop_id
from rwmod.mod_rules import (
    ANCHOR_PACKAGE_IDS,
    OFFICIAL_PACKAGE_IDS,
    build_package_id_index,
    find_explicit_conflicts,
    find_rule_conflicts,
    load_user_rules,
    merge_rule_sources,
    normalize_package_id,
    normalize_version,
    parse_rule_document,
    select_versioned_dependencies,
    select_versioned_ids,
)
from rwmod.version_support import detect_rimworld_version
from rwmod.xmlutil import parse_xml_root

# Safe XML parser: rejects entity-expansion / external-entity (XXE) attacks.

_log = logging.getLogger(__name__)

# ── rule data ──────────────────────────────────────────────────────
# (extracted from inline for discoverability; kept as a constant,
# not an external file, to keep the single-module distribution simple)

# packageIds of Harmony — the mod loader framework
_HARMONY_IDS: frozenset[str] = frozenset({"brrainz.harmony", "harmony"})

# Core + official DLC packageIds (ordered by required position). Matched
# exactly after normalization: a substring match made "ludeon.rimworld" hit
# "ludeon.rimworld.royalty" and produced false ordering warnings.
_CORE_DLC_IDS: tuple[str, ...] = tuple(normalize_package_id(pid) for pid in OFFICIAL_PACKAGE_IDS)
_CORE_DLC_SET = frozenset(_CORE_DLC_IDS)

# Known incompatible mod pairs: (keyword_a, keyword_b, reason)
# Keywords match case-insensitively against packageId
_KNOWN_CONFLICTS: tuple[tuple[str, str, str], ...] = (
    ("combat_extended", "yayo.combat", "Combat Extended 与 Yayo's Combat 冲突，二者择一"),
    ("combat_extended", "runandgun", "Combat Extended 与 RunAndGun 存在已知冲突，需兼容补丁"),
    ("pick.up.and.haul", "while.youre.up", "Pick Up And Haul 与 While You're Up 功能重叠"),
    ("dub's.bad.hygiene", "dubs.bad.hygiene.lite", "Dubs Bad Hygiene 与 Lite 版不可共存"),
)


def check_load_order(
    modsconfig_path: Path,
    mods_dir: Path,
    *,
    community_rules: Mapping[str, Any] | None = None,
    user_rules: Mapping[str, Any] | None = None,
    user_conflicts: list[dict] | None = None,
    rimworld_version: str | None = None,
    enable_steam_db: bool = False,
) -> dict:
    """Analyze ModsConfig.xml load order for common issues.

    On top of the fixed Harmony/Core/DLC checks, this compares the *declared*
    load rules against the actual order. Rule sources follow the same priority
    as sorting: user (``~/.rwmod/rules.json``) > community (cached Community
    Rules database) > built-in (installed About.xml).

    Raises ModNotFoundError when the file is absent and ValidationError when it
    is unreadable — "cannot analyze" is a failure, not a result with an `error`
    key, so callers get one failure channel (see the failure contract in
    CLAUDE.md).

    ``enable_steam_db`` opts into cached Steam Workshop metadata (the opt-in
    ``Config.enable_steam_db`` flag). It only reads ``~/.rwmod/steam_db.json``
    — this path never downloads. When disabled or the cache is absent the
    response is unchanged.

    Returns:
        {
            "total_mods": int,
            "issues": [{"severity": "error|warn|info", "message": str}],
            "load_order": [package_id, ...],
            "ok": bool,
            # only when enable_steam_db and a usable cache exists:
            "steam_db": {
                "matched": int,
                "game_versions": {package_id: [version, ...]},
                "unsupported": [{"mod": package_id, "game_versions": [...]}],
            },
        }
    """
    if not modsconfig_path.exists():
        raise ModNotFoundError(f"ModsConfig.xml 未找到: {modsconfig_path}")

    try:
        root = parse_xml_root(modsconfig_path)
        active = root.find("activeMods")
        if active is None:
            raise ValidationError("ModsConfig.xml 中没有 activeMods")
        load_order = [li.text or "" for li in active.findall("li") if li.text]
        config_version = normalize_version(root.findtext("version", "") or "")
    except RwmodError:
        raise
    except Exception as e:
        raise ValidationError(f"解析 ModsConfig.xml 失败: {e}") from e

    if not load_order:
        raise ValidationError("加载顺序为空")

    version = rimworld_version or config_version or _detect_mods_version(mods_dir)
    merged, explicit_conflicts = _load_rule_sources(
        mods_dir, community_rules, user_rules, user_conflicts
    )

    issues: list[dict] = []

    # 1. Check Harmony position
    _check_harmony(load_order, issues)

    # 2. Check Core/DLC order
    _check_core_dlc(load_order, issues)

    # 3. Check known conflicts (rules + legacy DB + hardcoded fallback)
    _check_known_conflicts(load_order, mods_dir, issues, merged, explicit_conflicts)

    # 4. Check declared rules against the actual order
    _check_declared_rules(load_order, merged, version, issues)

    # 5. Check for duplicate entries
    _check_duplicates(load_order, issues)

    result = {
        "total_mods": len(load_order),
        "issues": issues,
        "load_order": load_order,
        "ok": len([i for i in issues if i["severity"] == "error"]) == 0,
    }

    if enable_steam_db:
        steam = _steam_db_summary(load_order, mods_dir, version)
        if steam is not None:
            result["steam_db"] = steam

    return result


def _steam_db_summary(
    load_order: list[str], mods_dir: Path, rimworld_version: str
) -> dict[str, Any] | None:
    """Attach cached Steam Workshop gameVersions; ``None`` when unavailable.

    Disk-only by design: an opt-in flag must never turn a diagnostic request
    into a 47 MB download. Mods are matched by packageId first and by their
    installed ``PublishedFileId.txt`` second (many steamDB entries carry no
    packageId).
    """
    try:
        from rwmod.steam_db import SteamDB

        db = SteamDB.get()
        if not db.load_cached():
            return None
        pkg_to_wid = {
            normalize_package_id(pkg): wid
            for pkg, wid in package_id_to_workshop_id(mods_dir).items()
        }
    except Exception:  # noqa: BLE001 — enrichment is best-effort, never fatal
        return None

    target = normalize_version(rimworld_version)
    game_versions: dict[str, list[str]] = {}
    unsupported: list[dict[str, Any]] = []
    for pid in load_order:
        norm = normalize_package_id(pid)
        info = db.query_package(norm) if norm else None
        if info is None:
            workshop_id = pkg_to_wid.get(norm, "")
            if not workshop_id:
                continue
            info = db.query(workshop_id)
        if info is None:
            continue
        versions = list(info["game_versions"])
        game_versions[pid] = versions
        if target and versions and target not in versions:
            unsupported.append({"mod": pid, "game_versions": versions})

    return {
        "matched": len(game_versions),
        "game_versions": game_versions,
        "unsupported": unsupported,
    }


def _detect_mods_version(mods_dir: Path) -> str:
    for candidate in (mods_dir.parent, mods_dir):
        try:
            version = detect_rimworld_version(candidate)
        except (OSError, UnicodeDecodeError):
            version = None
        if version:
            return version
    return ""


def _load_rule_sources(
    mods_dir: Path,
    community_rules: Mapping[str, Any] | None,
    user_rules: Mapping[str, Any] | None,
    user_conflicts: list[dict] | None,
) -> tuple[dict[str, dict], list[dict]]:
    """Load and merge user > community > built-in rules (all disk-only)."""
    if user_rules is None:
        user_map, file_conflicts = load_user_rules()
    else:
        user_map, file_conflicts = parse_rule_document(user_rules)
    if user_conflicts:
        file_conflicts = [*file_conflicts, *user_conflicts]

    if community_rules is None:
        from rwmod.rimpy_db import RimPyDB

        try:
            db = RimPyDB.get()
            db.load_cached()
            community_map = dict(db.rules)
        except Exception:
            # A broken/injected DB must never block diagnostics.
            community_map = {}
    else:
        community_map, _ = parse_rule_document(community_rules)

    builtin: dict[str, dict] = {}
    if mods_dir.is_dir():
        from rwmod.rimsort import _collect_load_rules

        builtin = _collect_load_rules(mods_dir)

    return merge_rule_sources(user_map, community_map, builtin), file_conflicts


# ── rule checkers ──────────────────────────────────────────────────


def _check_harmony(order: list[str], issues: list[dict]) -> None:
    """Harmony must be at position 0 in the load order."""
    harmony_positions = [
        i
        for i, pid in enumerate(order)
        if any(hid in normalize_package_id(pid) for hid in _HARMONY_IDS)
    ]
    if not harmony_positions:
        issues.append(
            {
                "severity": "warn",
                "message": "未检测到 Harmony——大多数 Mod 需要 Harmony 才能运行",
            }
        )
    elif harmony_positions[0] != 0:
        issues.append(
            {
                "severity": "error",
                "message": f"⚠ Harmony 必须排在第一位！当前位置: #{harmony_positions[0] + 1}",
            }
        )


def _check_core_dlc(order: list[str], issues: list[dict]) -> None:
    """Core + DLCs should be at the top, before community mods."""
    core_positions: dict[str, int] = {}
    for i, pid in enumerate(order):
        pid_norm = normalize_package_id(pid)
        if pid_norm in _CORE_DLC_SET:
            core_positions[pid_norm] = i

    # Check Core is before DLCs
    core_dlc_order = [c for c in _CORE_DLC_IDS if c in core_positions]
    for j in range(1, len(core_dlc_order)):
        prev = core_dlc_order[j - 1]
        curr = core_dlc_order[j]
        if core_positions.get(prev, 0) > core_positions.get(curr, 999):
            issues.append(
                {
                    "severity": "warn",
                    "message": f"官方内容顺序建议: {prev} 应在 {curr} 之前",
                }
            )

    # Check first community mod comes after all Core/DLC
    if core_positions:
        max_core = max(core_positions.values())
        first_community = -1
        for i, pid in enumerate(order):
            pid_norm = normalize_package_id(pid)
            # Harmony is anchored before Core by convention, so it is not the
            # "community mod before official content" this check looks for.
            if pid_norm in _CORE_DLC_SET or pid_norm in _HARMONY_IDS:
                continue
            first_community = i
            break
        if first_community >= 0 and first_community < max_core:
            pid = order[first_community]
            short = pid.rsplit(".", 1)[-1] if "." in pid else pid[:30]
            issues.append(
                {
                    "severity": "warn",
                    "message": f"社区 Mod「{short}」排在官方内容之前，建议将官方 DLC 放在前面",
                }
            )


def _check_known_conflicts(
    order: list[str],
    mods_dir: Path,
    issues: list[dict],
    rules: Mapping[str, Mapping[str, Any]] | None = None,
    explicit_conflicts: list[dict] | None = None,
) -> None:
    """Check for known incompatible mod pairs.

    Rule-declared incompatibilities (user > community > built-in) come first;
    the legacy RimPy database is consulted for workshop-id pairs, and a small
    hardcoded set covers a fully offline install with no cache.
    """
    pid_norm = [normalize_package_id(p) for p in order]
    found_any = False

    # 1. Rule-declared conflicts (packageId based).
    if rules:
        for conflict in find_rule_conflicts(rules, pid_norm):
            issues.append(
                {
                    "severity": "error",
                    "kind": "incompatibleWith",
                    "message": (
                        f"⚠ 「{conflict['a']}」与「{conflict['b']}」不兼容: {conflict['reason']}"
                    ),
                }
            )
            found_any = True
    for conflict in find_explicit_conflicts(explicit_conflicts or [], pid_norm):
        issues.append(
            {
                "severity": "error",
                "kind": "userConflict",
                "message": (
                    f"⚠ 「{conflict['a']}」与「{conflict['b']}」被用户规则标记为冲突: "
                    f"{conflict['reason']}"
                ),
            }
        )
        found_any = True

    # 2. Legacy RimPy database (workshop-id keyed). Read from disk only:
    # diagnostics must work offline and never stall on a download.
    try:
        from rwmod.rimpy_db import RimPyDB

        db = RimPyDB.get()
        loaded = db.load_cached() if hasattr(db, "load_cached") else db.ensure_loaded()
        # Collect workshop IDs from installed mods. packageIds are
        # case-insensitive, hence the normalized keys.
        pkg_to_wid = {
            normalize_package_id(pkg): wid
            for pkg, wid in package_id_to_workshop_id(mods_dir).items()
        }

        active_wids = {pkg_to_wid[p] for p in pid_norm if p in pkg_to_wid}
        if active_wids and loaded:
            db_conflicts = db.get_conflicts(active_wids)
            for c in db_conflicts:
                issues.append(
                    {
                        "severity": "error",
                        "kind": "rimpyConflict",
                        "message": f"⚠ {c['reason']}",
                    }
                )
                found_any = True
    except Exception:
        pass

    if found_any:
        return  # Use rule/DB results, skip hardcoded fallback

    # 3. Fallback: hardcoded known conflicts (offline / DB unavailable)
    for a_keyword, b_keyword, reason in _KNOWN_CONFLICTS:
        a_found = [i for i, p in enumerate(pid_norm) if a_keyword in p]
        b_found = [i for i, p in enumerate(pid_norm) if b_keyword in p]
        if a_found and b_found:
            issues.append(
                {
                    "severity": "error",
                    "message": f"⚠ {reason}",
                }
            )


# ── declared rules vs actual order ─────────────────────────────────


def _after_targets(rule: Mapping[str, Any], version: str) -> set[str]:
    """Ids the owner declares it must follow."""
    forced = {normalize_package_id(x) for x in rule.get("forceLoadAfter") or ()}
    normal = {normalize_package_id(x) for x in rule.get("loadAfter") or ()}
    versioned = select_versioned_ids(rule.get("loadAfterByVersion") or {}, version)
    return {x for x in forced | normal | versioned if x}


def _before_targets(rule: Mapping[str, Any], version: str) -> set[str]:
    """Ids the owner declares it must precede."""
    forced = {normalize_package_id(x) for x in rule.get("forceLoadBefore") or ()}
    normal = {normalize_package_id(x) for x in rule.get("loadBefore") or ()}
    versioned = select_versioned_ids(rule.get("loadBeforeByVersion") or {}, version)
    return {x for x in forced | normal | versioned if x}


def _dependency_targets(rule: Mapping[str, Any], version: str) -> list[tuple[str, ...]]:
    groups = list(rule.get("dependencies") or [])
    groups += select_versioned_dependencies(rule.get("dependenciesByVersion") or {}, version)
    return groups


def _check_declared_rules(
    order: list[str],
    rules: Mapping[str, Mapping[str, Any]],
    version: str,
    issues: list[dict],
) -> None:
    """Report every declared rule the actual load order violates.

    ``loadAfter``/``loadBefore``/dependency violations are warnings (the game
    may still boot); ``forceLoadAfter``/``forceLoadBefore`` violations are
    errors because RimSort treats them as hard edges. ``loadTop`` /
    ``loadBottom`` are checked relative to community mods only — official
    content and Harmony are anchored elsewhere.
    """
    display = build_package_id_index(order)
    positions: dict[str, int] = {}
    for i, pid in enumerate(order):
        norm = normalize_package_id(pid)
        if norm and norm not in positions:
            positions[norm] = i
    active = set(positions)
    anchor_norms = {normalize_package_id(pid) for pid in ANCHOR_PACKAGE_IDS}
    emitted: set[tuple[str, str]] = set()

    def _emit(kind: str, a: str, b: str, message: str, severity: str) -> None:
        key = (kind, f"{a}|{b}")
        if key in emitted:
            return
        emitted.add(key)
        issues.append({"severity": severity, "kind": kind, "message": message})

    for owner in active:
        rule = rules.get(owner)
        if not isinstance(rule, Mapping):
            continue
        owner_display = display[owner]
        owner_pos = positions[owner]

        after = _after_targets(rule, version)
        for target in after:
            if target not in active or target == owner:
                continue
            if owner_pos < positions[target]:
                forced = target in {
                    normalize_package_id(x) for x in rule.get("forceLoadAfter") or ()
                }
                kind = "forceLoadAfter" if forced else "loadAfter"
                severity = "error" if kind.startswith("force") else "warn"
                _emit(
                    kind,
                    owner,
                    target,
                    f"「{owner_display}」声明需在「{display[target]}」之后加载，但当前顺序相反",
                    severity,
                )

        before = _before_targets(rule, version)
        for target in before:
            if target not in active or target == owner:
                continue
            if owner_pos > positions[target]:
                forced = target in {
                    normalize_package_id(x) for x in rule.get("forceLoadBefore") or ()
                }
                kind = "forceLoadBefore" if forced else "loadBefore"
                severity = "error" if kind.startswith("force") else "warn"
                _emit(
                    kind,
                    owner,
                    target,
                    f"「{owner_display}」声明需在「{display[target]}」之前加载，但当前顺序相反",
                    severity,
                )

        for group in _dependency_targets(rule, version):
            present = [member for member in group if member in active]
            if not present or any(positions[member] < owner_pos for member in present):
                continue
            target = present[0]
            _emit(
                "dependency",
                owner,
                target,
                f"「{owner_display}」依赖「{display[target]}」，但「{display[target]}」未在其之前加载",
                "warn",
            )

        if rule.get("loadTop") and owner not in anchor_norms:
            earlier = [
                pid
                for pid in active
                if pid not in anchor_norms and pid != owner and positions[pid] < owner_pos
            ]
            if earlier:
                _emit(
                    "loadTop",
                    owner,
                    "",
                    f"「{owner_display}」声明应优先加载（loadTop），但前面还有社区 Mod",
                    "warn",
                )

        if rule.get("loadBottom") and owner not in anchor_norms:
            later = [
                pid
                for pid in active
                if pid not in anchor_norms and pid != owner and positions[pid] > owner_pos
            ]
            if later:
                _emit(
                    "loadBottom",
                    owner,
                    "",
                    f"「{owner_display}」声明应最后加载（loadBottom），但后面还有社区 Mod",
                    "warn",
                )


def _check_duplicates(order: list[str], issues: list[dict]) -> None:
    """Check for duplicate packageIds in load order.

    Comparison is case-insensitive: RimWorld lowercases packageIds, so
    ``Mod.A`` and ``mod.a`` are the same mod.
    """
    seen: dict[str, int] = {}
    for i, pid in enumerate(order):
        key = normalize_package_id(pid)
        if key in seen:
            short = pid.rsplit(".", 1)[-1] if "." in pid else pid[:30]
            issues.append(
                {
                    "severity": "warn",
                    "message": f"重复条目「{short}」出现在 #{seen[key] + 1} 和 #{i + 1}",
                }
            )
        else:
            seen[key] = i
