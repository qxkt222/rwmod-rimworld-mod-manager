"""Tests for rimsort.py — ModsConfig.xml generation, parsing, comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rwmod.mod_rules import (
    find_explicit_conflicts,
    find_rule_conflicts,
    load_user_rules,
    merge_rule_sources,
    parse_rule_document,
)
from rwmod.rimsort import (
    _collect_load_rules,
    _rule_edges,
    compare_modsconfig,
    generate_modsconfig,
    parse_modsconfig,
    resolve_missing_workshop_ids,
    sort_mods,
)


def _make_mod(
    mods_dir: Path,
    folder: str,
    package_id: str,
    workshop_id: str = "",
    extra: str = "",
) -> None:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><packageId>{package_id}</packageId>{extra}</ModMetaData>",
        encoding="utf-8",
    )
    if workshop_id:
        (about / "PublishedFileId.txt").write_text(workshop_id, encoding="utf-8")


class TestGenerateModsconfig:
    def test_generates_xml(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        _make_mod(mods_dir, "A", "ludeon.rimworld")
        _make_mod(mods_dir, "B", "brrainz.harmony")
        xml = generate_modsconfig(mods_dir)
        assert "<ModsConfigData>" in xml
        assert "brrainz.harmony" in xml
        assert "ludeon.rimworld" in xml

    def test_writes_output_file(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        out = tmp_path / "ModsConfig.xml"
        xml = generate_modsconfig(mods_dir, out)
        assert out.exists()
        assert out.read_text(encoding="utf-8") == xml


class TestParseModsconfig:
    def test_parse(self, tmp_path: Path):
        p = tmp_path / "ModsConfig.xml"
        p.write_text(
            "<ModsConfigData><version>1.0.0</version><activeMods>"
            "<li>a.mod</li><li>b.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        data = parse_modsconfig(p)
        assert data["active_mods"] == ["a.mod", "b.mod"]
        assert data["total"] == 2

    def test_missing_file(self, tmp_path: Path):
        assert "error" in parse_modsconfig(tmp_path / "nope.xml")


class TestCompareModsconfig:
    def test_compare(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        _make_mod(mods_dir, "A", "a.mod", "111")
        _make_mod(mods_dir, "B", "b.mod", "222")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>a.mod</li><li>missing.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        data = compare_modsconfig(cfg, mods_dir)
        assert data["missing"] == ["missing.mod"]
        assert set(data["installed"]) == {"a.mod"}
        assert data["extra_count"] == 1

    def test_compare_missing_config(self, tmp_path: Path):
        data = compare_modsconfig(tmp_path / "nope.xml", tmp_path / "Mods")
        assert "error" in data


class TestResolveMissingWorkshopIds:
    def test_resolves(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        _make_mod(mods_dir, "A", "a.mod", "111")
        results = resolve_missing_workshop_ids(["a.mod", "ghost.mod"], mods_dir)
        assert results[0]["resolved"] is True
        assert results[0]["workshop_id"] == "111"
        assert results[1]["resolved"] is False

    def test_resolves_case_insensitively(self, tmp_path: Path):
        """S-P0-2: ModsConfig ids may be lowercase while About.xml is mixed."""
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        _make_mod(mods_dir, "A", "Author.Mod", "111")
        results = resolve_missing_workshop_ids(["author.mod"], mods_dir)
        assert results[0]["resolved"] is True
        assert results[0]["workshop_id"] == "111"


# ── S-P0-2: packageId comparisons are case-insensitive ──────────────


class TestCaseInsensitivePackageIds:
    def test_sort_matches_active_ids_case_insensitively(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "Author.Mod",
            extra="<loadAfter><li>OTHER.mod</li></loadAfter>",
        )
        _make_mod(tmp_path, "B", "Other.Mod")

        result = sort_mods(tmp_path, ["AUTHOR.MOD", "other.mod"])

        assert result["unresolved"] == []
        # Output keeps the caller's (ModsConfig) spelling.
        assert set(result["sorted"]) == {"AUTHOR.MOD", "other.mod"}
        assert result["sorted"].index("other.mod") < result["sorted"].index("AUTHOR.MOD")

    def test_collect_rules_normalizes_declared_targets(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "Author.Mod",
            extra="<loadAfter><li>OTHER.Mod</li></loadAfter>",
        )
        rules = _collect_load_rules(tmp_path)
        # Keys keep the original spelling; edge targets are normalized.
        assert rules["Author.Mod"]["loadAfter"] == {"other.mod"}

    def test_compare_is_case_insensitive(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        _make_mod(mods_dir, "A", "Author.Mod", "111")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>author.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        data = compare_modsconfig(cfg, mods_dir)

        assert data["missing"] == []
        assert data["extra"] == []
        assert data["installed"] == ["Author.Mod"]

    def test_compare_core_package_id_is_exempt(self, tmp_path: Path):
        """PARSE-7: Core's real packageId must never be reported missing."""
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        _make_mod(mods_dir, "A", "a.mod")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods>"
            "<li>ludeon.rimworld</li><li>a.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        data = compare_modsconfig(cfg, mods_dir)

        assert data["missing"] == []


_REAL_CONFIG = (
    Path.home()
    / "AppData"
    / "LocalLow"
    / "Ludeon Studios"
    / "RimWorld by Ludeon Studios"
    / "Config"
    / "ModsConfig.xml"
)
_REAL_MODS = Path("D:/RimWorld/Mods")


@pytest.mark.skipif(
    not _REAL_CONFIG.exists() or not _REAL_MODS.is_dir(),
    reason="local RimWorld install not present",
)
def test_compare_real_modsconfig_only_official_content_missing(tmp_path: Path):
    """S-P0-2 regression: a real 522-mod config must not report community mods
    as missing just because ModsConfig lowercases packageIds."""
    import shutil

    cfg = tmp_path / "ModsConfig.xml"
    shutil.copy2(_REAL_CONFIG, cfg)

    data = compare_modsconfig(cfg, _REAL_MODS)

    non_official = [pid for pid in data["missing"] if not pid.lower().startswith("ludeon.rimworld")]
    assert non_official == []
    assert len(data["missing"]) <= 6  # Core + up to 5 DLCs (Core is exempted)


# ── S-P0-3: official content / Harmony anchoring ────────────────────

ANCHOR_ORDER = [
    "brrainz.harmony",
    "ludeon.rimworld",
    "ludeon.rimworld.royalty",
    "ludeon.rimworld.ideology",
    "ludeon.rimworld.biotech",
    "ludeon.rimworld.anomaly",
    "ludeon.rimworld.odyssey",
]


class TestOfficialAnchoring:
    def test_sort_anchors_harmony_then_core_then_dlc(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "H", "brrainz.harmony")
        _make_mod(
            mods,
            "A",
            "a.mod",
            extra="<loadAfter><li>brrainz.harmony</li></loadAfter>",
        )
        active = [
            "a.mod",
            "ludeon.rimworld.odyssey",
            "ludeon.rimworld.anomaly",
            "ludeon.rimworld.biotech",
            "ludeon.rimworld.ideology",
            "ludeon.rimworld.royalty",
            "ludeon.rimworld",
            "brrainz.harmony",
        ]

        result = sort_mods(mods, active)

        assert result["unresolved"] == []
        assert result["sorted"] == ANCHOR_ORDER + ["a.mod"]

    def test_anchor_preserves_input_spelling(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "H", "Brrainz.Harmony")
        result = sort_mods(mods, ["BRRAINZ.HARMONY", "LUDEON.RIMWORLD"])
        assert result["sorted"] == ["BRRAINZ.HARMONY", "LUDEON.RIMWORLD"]
        assert result["unresolved"] == []

    def test_non_anchor_unknown_ids_stay_unresolved(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod")
        result = sort_mods(mods, ["a.mod", "someone.unknown"])
        assert result["sorted"] == ["a.mod"]
        assert result["unresolved"] == ["someone.unknown"]

    @pytest.mark.skipif(
        not _REAL_CONFIG.exists() or not _REAL_MODS.is_dir(),
        reason="local RimWorld install not present",
    )
    def test_real_config_sorts_all_522_with_no_unresolved(self):
        active = parse_modsconfig(_REAL_CONFIG)["active_mods"]
        result = sort_mods(_REAL_MODS, active)
        # The live install grows over time; the invariant is that every active
        # id is placed exactly once, not that the player owns 522 mods.
        assert len(result["sorted"]) == len(active)
        assert result["unresolved"] == []
        assert result["sorted"][: len(ANCHOR_ORDER)] == ANCHOR_ORDER

    @pytest.mark.skipif(
        not _REAL_CONFIG.exists() or not _REAL_MODS.is_dir(),
        reason="local RimWorld install not present",
    )
    def test_real_apply_keeps_every_id_and_anchors_official(self, tmp_path: Path):
        """Apply against a tmp copy of the real config: no real file is touched."""
        import shutil

        from rwmod.config import Config
        from rwmod.rimsort import _collect_load_rules
        from rwmod.routers.rimsort import rimsort_apply

        cfg_path = tmp_path / "ModsConfig.xml"
        shutil.copy2(_REAL_CONFIG, cfg_path)
        original = parse_modsconfig(cfg_path)["active_mods"]

        cfg = Config(mods_dir=_REAL_MODS, rimworld_dir=tmp_path)
        result = rimsort_apply(cfg=cfg)

        assert result["ok"] is True
        written = parse_modsconfig(cfg_path)["active_mods"]
        assert len(written) == len(original)
        assert written[: len(ANCHOR_ORDER)] == ANCHOR_ORDER
        assert {p.lower() for p in written} == {p.lower() for p in original}

        # Normal loadAfter edges among installed mods are still respected.
        rules = _collect_load_rules(_REAL_MODS)
        norms = {p.lower(): p for p in written}
        index = {p.lower(): i for i, p in enumerate(written)}
        checked = 0
        for pid, rule in rules.items():
            pid_norm = pid.lower()
            if pid_norm not in index:
                continue
            for dep in rule["loadAfter"]:
                if dep in index and not dep.startswith("ludeon.rimworld"):
                    assert index[dep] < index[pid_norm], (dep, pid, written)
                    checked += 1
                    if checked >= 25:
                        break
            if checked >= 25:
                break
        assert checked > 0
        assert norms


# ── S-P0-3: generate writes real version / knownExpansions ──────────


class TestGenerateOfficialContent:
    def _fake_rimworld(self, tmp_path: Path) -> Path:
        rw = tmp_path / "RimWorld"
        (rw / "Data" / "Core" / "About").mkdir(parents=True)
        (rw / "Data" / "Core" / "About" / "About.xml").write_text(
            "<ModMetaData><packageId>Ludeon.RimWorld</packageId></ModMetaData>",
            encoding="utf-8",
        )
        (rw / "Data" / "Royalty" / "About").mkdir(parents=True)
        (rw / "Data" / "Royalty" / "About" / "About.xml").write_text(
            "<ModMetaData><packageId>Ludeon.RimWorld.Royalty</packageId></ModMetaData>",
            encoding="utf-8",
        )
        (rw / "Version.txt").write_text("1.6.4850 rev645", encoding="utf-8")
        return rw

    def test_generate_writes_real_version_and_expansions(self, tmp_path: Path):
        rw = self._fake_rimworld(tmp_path)
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod")

        xml = generate_modsconfig(mods, rimworld_dir=rw)

        assert "<version>1.6.4850 rev645</version>" in xml
        assert "1.0.0" not in xml
        assert "buildNumber" not in xml
        assert "<knownExpansions>" in xml
        assert "ludeon.rimworld.royalty" in xml.lower()
        assert "ludeon.rimworld" in xml.lower()
        # Official content is anchored before community mods.
        assert xml.lower().index("ludeon.rimworld") < xml.lower().index("a.mod")

    def test_generate_without_rimworld_dir_omits_unknown_version(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod")
        xml = generate_modsconfig(mods)
        assert "1.0.0" not in xml
        assert "buildNumber" not in xml


# ── Wave 3: community / user rule sources ──────────────────────────


def _named_mod(mods_dir: Path, folder: str, package_id: str, name: str, extra: str = "") -> None:
    _make_mod(mods_dir, folder, package_id, extra=f"<name>{name}</name>{extra}")


class TestCommunityRuleSource:
    def test_community_load_after_orders_mods(self, tmp_path: Path):
        _named_mod(tmp_path, "A", "a.mod", "Zed")
        _named_mod(tmp_path, "B", "b.mod", "Alpha")
        community = {"a.mod": {"loadAfter": {"b.mod": {}}}}

        result = sort_mods(tmp_path, ["a.mod", "b.mod"], community_rules=community)

        assert result["sorted"] == ["b.mod", "a.mod"]
        assert result["unresolved"] == []

    def test_community_load_before_orders_mods(self, tmp_path: Path):
        _named_mod(tmp_path, "A", "a.mod", "Zed")
        _named_mod(tmp_path, "B", "b.mod", "Alpha")
        community = {"a.mod": {"loadBefore": {"b.mod": {}}}}

        result = sort_mods(tmp_path, ["a.mod", "b.mod"], community_rules=community)

        assert result["sorted"] == ["a.mod", "b.mod"]

    def test_community_load_top_and_bottom(self, tmp_path: Path):
        _named_mod(tmp_path, "A", "a.mod", "Zed")
        _named_mod(tmp_path, "B", "b.mod", "Alpha")
        community = {
            "a.mod": {"loadTop": {"value": True}},
            "b.mod": {"loadBottom": {"value": True}},
        }

        result = sort_mods(tmp_path, ["a.mod", "b.mod"], community_rules=community)

        assert result["sorted"] == ["a.mod", "b.mod"]

    def test_load_top_wins_when_a_mod_declares_both(self, tmp_path: Path):
        _named_mod(tmp_path, "A", "a.mod", "Zed")
        _named_mod(tmp_path, "B", "b.mod", "Alpha")
        community = {"a.mod": {"loadTop": {"value": True}, "loadBottom": {"value": True}}}

        result = sort_mods(tmp_path, ["a.mod", "b.mod"], community_rules=community)

        assert result["sorted"] == ["a.mod", "b.mod"]
        assert len(result["sorted"]) == 2

    def test_community_rule_ids_are_case_insensitive(self, tmp_path: Path):
        _named_mod(tmp_path, "A", "Author.Mod", "Zed")
        _named_mod(tmp_path, "B", "Other.Mod", "Alpha")
        community = {"AUTHOR.MOD": {"loadAfter": {"OTHER.MOD": {}}}}

        result = sort_mods(tmp_path, ["author.mod", "other.mod"], community_rules=community)

        assert result["sorted"] == ["other.mod", "author.mod"]

    def test_an_already_parsed_rule_map_can_be_injected(self, tmp_path: Path):
        _named_mod(tmp_path, "A", "a.mod", "Zed")
        _named_mod(tmp_path, "B", "b.mod", "Alpha")
        rules, _ = parse_rule_document({"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}})

        result = sort_mods(tmp_path, ["a.mod", "b.mod"], community_rules=rules)

        assert result["sorted"] == ["b.mod", "a.mod"]

    def test_default_source_reads_cached_community_rules_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """sort_mods must never reach the network: the cached DB is the source."""
        from rwmod import rimpy_db as rimpy_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("sort_mods attempted a network download")

        monkeypatch.setattr(rimpy_mod, "urlopen", boom)
        cache_dir = Path.home() / ".rwmod"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "community_rules.json").write_text(
            json.dumps({"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}}),
            encoding="utf-8",
        )
        mods = tmp_path / "Mods"
        mods.mkdir()
        _named_mod(mods, "A", "a.mod", "Zed")
        _named_mod(mods, "B", "b.mod", "Alpha")

        result = sort_mods(mods, ["a.mod", "b.mod"])

        assert result["sorted"] == ["b.mod", "a.mod"]


class TestUserRuleSource:
    def test_user_rules_override_community_for_the_same_owner_field(self):
        user_doc = {"rules": {"a.mod": {"loadAfter": {"c.mod": {}}}}}
        community_doc = {"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}}
        user, _ = parse_rule_document(user_doc)
        community, _ = parse_rule_document(community_doc)

        merged = merge_rule_sources(user, community, {})

        assert merged["a.mod"]["loadAfter"] == {"c.mod"}

    def test_community_fills_fields_the_user_did_not_declare(self):
        user_doc = {"rules": {"a.mod": {"loadTop": {"value": True}}}}
        community_doc = {"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}}
        user, _ = parse_rule_document(user_doc)
        community, _ = parse_rule_document(community_doc)

        merged = merge_rule_sources(user, community, {})

        assert merged["a.mod"]["loadTop"] is True
        assert merged["a.mod"]["loadAfter"] == {"b.mod"}

    def test_builtin_rules_are_the_lowest_priority(self):
        community_doc = {"rules": {"a.mod": {"loadAfter": {"c.mod": {}}}}}
        community, _ = parse_rule_document(community_doc)
        builtin = {"a.mod": {"loadAfter": {"b.mod"}, "name": "Builtin"}}

        merged = merge_rule_sources(community, builtin)

        assert merged["a.mod"]["loadAfter"] == {"c.mod"}
        assert merged["a.mod"]["name"] == "Builtin"

    def test_load_user_rules_file_and_missing_file_is_empty(self, tmp_path: Path):
        missing = tmp_path / "rules.json"
        assert load_user_rules(missing) == ({}, [])

        doc = {
            "rules": {"a.mod": {"loadBefore": {"b.mod": {}}}},
            "conflicts": [{"a": "x.mod", "b": "y.mod", "reason": "nope"}],
        }
        missing.write_text(json.dumps(doc), encoding="utf-8")

        rules, conflicts = load_user_rules(missing)

        assert rules["a.mod"]["loadBefore"] == {"b.mod"}
        assert conflicts == [{"a": "x.mod", "b": "y.mod", "reason": "nope"}]

    def test_malformed_user_rules_fail_open(self, tmp_path: Path):
        bad = tmp_path / "rules.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_user_rules(bad) == ({}, [])

    def test_user_rules_file_drives_sort_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        (home / ".rwmod").mkdir(parents=True)
        (home / ".rwmod" / "rules.json").write_text(
            json.dumps({"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}}),
            encoding="utf-8",
        )
        mods = tmp_path / "Mods"
        mods.mkdir()
        _named_mod(mods, "A", "a.mod", "Zed")
        _named_mod(mods, "B", "b.mod", "Alpha")

        result = sort_mods(mods, ["a.mod", "b.mod"])

        assert result["sorted"] == ["b.mod", "a.mod"]


class TestRuleConflictHelpers:
    def test_parse_community_incompatible_with(self):
        rules, conflicts = parse_rule_document(
            {"rules": {"a.mod": {"incompatibleWith": {"b.mod": {"name": "B", "comment": "no"}}}}}
        )
        assert rules["a.mod"]["incompatibleWith"] == {"b.mod"}
        assert conflicts == []

    def test_find_rule_conflicts_dedupes_symmetric_pairs(self):
        rules, _ = parse_rule_document(
            {
                "rules": {
                    "a.mod": {"incompatibleWith": {"b.mod": {}}},
                    "b.mod": {"incompatibleWith": {"a.mod": {}}},
                }
            }
        )
        found = find_rule_conflicts(rules, {"a.mod", "b.mod", "c.mod"})
        assert len(found) == 1
        assert {found[0]["a"], found[0]["b"]} == {"a.mod", "b.mod"}

    def test_explicit_conflicts_respect_active_set(self):
        _, conflicts = parse_rule_document(
            {"conflicts": [{"a": "a.mod", "b": "b.mod", "reason": "用户禁配"}]}
        )
        assert find_explicit_conflicts(conflicts, {"a.mod", "b.mod"})[0]["reason"] == "用户禁配"
        assert find_explicit_conflicts(conflicts, {"a.mod"}) == []


# ── Wave 3: dependencies, force edges, versioned rules ─────────────


class TestDependencyForceVersionedEdges:
    def test_mod_dependency_loads_before_dependent(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li>b.mod</li></modDependencies>",
        )
        _make_mod(tmp_path, "B", "b.mod")

        result = sort_mods(tmp_path, ["a.mod", "b.mod"])

        assert result["sorted"] == ["b.mod", "a.mod"]

    def test_rich_dependency_group_accepts_an_alternative(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra=(
                "<modDependencies><li><packageId>x.mod</packageId>"
                "<alternativePackageIds><li>y.mod</li></alternativePackageIds></li>"
                "</modDependencies>"
            ),
        )
        _make_mod(tmp_path, "Y", "y.mod")

        result = sort_mods(tmp_path, ["a.mod", "y.mod"])

        assert result["sorted"] == ["y.mod", "a.mod"]

    def test_force_load_after_creates_a_hard_edge(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<forceLoadAfter><li>b.mod</li></forceLoadAfter>")
        _make_mod(tmp_path, "B", "b.mod")

        edges = _rule_edges(_collect_load_rules(tmp_path), {"a.mod", "b.mod"})
        assert edges["a.mod"] == {"b.mod"}
        result = sort_mods(tmp_path, ["a.mod", "b.mod"])
        assert result["sorted"] == ["b.mod", "a.mod"]

    def test_force_load_before_creates_a_hard_edge(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod")
        _make_mod(tmp_path, "B", "b.mod", extra="<forceLoadBefore><li>a.mod</li></forceLoadBefore>")

        edges = _rule_edges(_collect_load_rules(tmp_path), {"a.mod", "b.mod"})
        assert edges["a.mod"] == {"b.mod"}
        result = sort_mods(tmp_path, ["a.mod", "b.mod"])
        assert result["sorted"] == ["b.mod", "a.mod"]

    def test_load_after_by_version_selects_the_matching_group(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra=(
                "<loadAfterByVersion><v1.5><li>b.mod</li></v1.5>"
                "<v1.6><li>c.mod</li></v1.6></loadAfterByVersion>"
            ),
        )
        _make_mod(tmp_path, "B", "b.mod")
        _make_mod(tmp_path, "C", "c.mod")
        rules = _collect_load_rules(tmp_path)

        assert rules["a.mod"]["loadAfterByVersion"] == {"1.5": {"b.mod"}, "1.6": {"c.mod"}}
        active = {"a.mod", "b.mod", "c.mod"}
        assert _rule_edges(rules, active, version="1.6")["a.mod"] == {"c.mod"}
        assert _rule_edges(rules, active, version="1.5")["a.mod"] == {"b.mod"}
        assert _rule_edges(rules, active, version=None)["a.mod"] == set()

    def test_mod_dependencies_by_version_selects_the_matching_group(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra=(
                "<modDependenciesByVersion><v1.5><li>b.mod</li></v1.5>"
                "<v1.6><li>c.mod</li></v1.6></modDependenciesByVersion>"
            ),
        )
        _make_mod(tmp_path, "B", "b.mod")
        _make_mod(tmp_path, "C", "c.mod")
        rules = _collect_load_rules(tmp_path)
        active = {"a.mod", "b.mod", "c.mod"}

        assert _rule_edges(rules, active, version="1.6")["a.mod"] == {"c.mod"}
        assert _rule_edges(rules, active, version="1.5")["a.mod"] == {"b.mod"}

    def test_default_version_is_read_from_version_txt_next_to_mods(self, tmp_path: Path):
        (tmp_path / "Version.txt").write_text("1.6.4850 rev645", encoding="utf-8")
        mods = tmp_path / "Mods"
        mods.mkdir()
        _named_mod(
            mods,
            "A",
            "a.mod",
            "Alpha",
            extra=(
                "<loadAfterByVersion><v1.5><li>b.mod</li></v1.5>"
                "<v1.6><li>c.mod</li></v1.6></loadAfterByVersion>"
            ),
        )
        _named_mod(mods, "B", "b.mod", "Bravo")
        _named_mod(mods, "C", "c.mod", "Charlie")

        result = sort_mods(mods)

        # v1.6 edge: c before a. b is unconstrained (would sort first by name).
        assert result["sorted"] == ["b.mod", "c.mod", "a.mod"]

    def test_missing_dependency_target_is_not_reported_unresolved(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li>ghost.mod</li></modDependencies>",
        )

        result = sort_mods(tmp_path, ["a.mod"])

        assert result["sorted"] == ["a.mod"]
        assert result["unresolved"] == []

    def test_cross_partition_edge_is_reported_not_dropped(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<loadTop>true</loadTop><loadAfter><li>b.mod</li></loadAfter>",
        )
        _make_mod(tmp_path, "B", "b.mod")

        result = sort_mods(tmp_path, ["a.mod", "b.mod"])

        # loadTop still wins (existing contract), but the edge is not silently lost.
        assert result["sorted"][0] == "a.mod"
        assert any(v["mod"] == "a.mod" and v["dependency"] == "b.mod" for v in result["violations"])

    def test_no_violations_when_rules_are_consistent(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        _make_mod(tmp_path, "B", "b.mod")

        result = sort_mods(tmp_path, ["a.mod", "b.mod"])

        assert result["violations"] == []
