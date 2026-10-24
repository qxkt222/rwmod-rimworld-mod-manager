"""Tests for load_order.py — mod ordering analysis."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from rwmod.errors import ModNotFoundError
from rwmod.load_order import check_load_order


def _write_modsconfig(path: Path, active_mods: list[str]) -> None:
    root = ET.Element("ModsConfigData")
    ET.SubElement(root, "version").text = "1.0"
    am = ET.SubElement(root, "activeMods")
    for pid in active_mods:
        ET.SubElement(am, "li").text = pid
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")


class TestCheckLoadOrder:
    def test_harmony_not_first(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["some.mod", "brrainz.harmony", "other.mod"])
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert not result.get("ok")
        assert any("Harmony" in i["message"] for i in result["issues"])

    def test_harmony_first_is_ok(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "some.mod"])
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert result["ok"]

    def test_duplicate_detected(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "mod.a", "mod.a"])
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert any("重复" in i["message"] for i in result["issues"])

    def test_missing_config_raises_not_found(self, tmp_path: Path):
        """A missing file is a failure, not a result carrying an `error` key."""
        with pytest.raises(ModNotFoundError):
            check_load_order(tmp_path / "nonexistent.xml", tmp_path / "Mods")

    def test_known_conflict(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "combat_extended", "yayo.combat"])
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert any("冲突" in i["message"] for i in result["issues"])

    def test_duplicate_is_case_insensitive(self, tmp_path: Path):
        """S-P0-2: RimWorld lowercases packageIds, so Mod.A and mod.a are the
        same mod and must be reported as a duplicate."""
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "Mod.A", "mod.a"])
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert any("重复" in i["message"] for i in result["issues"])

    def test_known_conflict_is_case_insensitive(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "Combat_Extended", "YAYO.COMBAT"])
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert any("冲突" in i["message"] for i in result["issues"])

    def test_core_before_royalty_has_no_false_positive(self, tmp_path: Path):
        """S-P0-3: exact matching — 'ludeon.rimworld' must not be seen inside
        'ludeon.rimworld.royalty' (the old substring bug)."""
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(
            cfg,
            [
                "brrainz.harmony",
                "ludeon.rimworld",
                "ludeon.rimworld.royalty",
                "ludeon.rimworld.ideology",
                "some.mod",
            ],
        )
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert not any("官方内容顺序建议" in i["message"] for i in result["issues"])
        assert not any("排在官方内容之前" in i["message"] for i in result["issues"])

    def test_odyssey_is_recognized_as_official(self, tmp_path: Path):
        """S-P0-3: Odyssey (1.6 DLC) must be in the official order."""
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(
            cfg,
            [
                "brrainz.harmony",
                "ludeon.rimworld",
                "ludeon.rimworld.royalty",
                "ludeon.rimworld.ideology",
                "ludeon.rimworld.biotech",
                "ludeon.rimworld.anomaly",
                "ludeon.rimworld.odyssey",
                "some.mod",
            ],
        )
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert not any("官方内容顺序建议" in i["message"] for i in result["issues"])
        assert not any("排在官方内容之前" in i["message"] for i in result["issues"])

    def test_core_after_royalty_warns(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "ludeon.rimworld.royalty", "ludeon.rimworld"])
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()

        result = check_load_order(cfg, mods_dir)
        assert any("官方内容顺序建议" in i["message"] for i in result["issues"])


# ── Wave 3: declared rules vs the actual order ─────────────────────


def _make_mod(mods_dir: Path, folder: str, package_id: str, extra: str = "") -> None:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{folder}</name><packageId>{package_id}</packageId>{extra}</ModMetaData>",
        encoding="utf-8",
    )


class TestDeclaredRuleDiagnostics:
    def test_load_after_violation_is_reported(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        _make_mod(mods, "B", "b.mod")
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "a.mod", "b.mod"])

        result = check_load_order(cfg, mods)

        violations = [
            i for i in result["issues"] if "a.mod" in i["message"] and "b.mod" in i["message"]
        ]
        assert violations, result["issues"]
        assert violations[0]["severity"] == "warn"

    def test_correct_load_after_has_no_violation(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        _make_mod(mods, "B", "b.mod")
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "b.mod", "a.mod"])

        result = check_load_order(cfg, mods)

        assert not any(
            "顺序相反" in i["message"] or "之后加载" in i["message"] for i in result["issues"]
        )

    def test_dependency_violation_is_reported(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod", extra="<modDependencies><li>b.mod</li></modDependencies>")
        _make_mod(mods, "B", "b.mod")
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "a.mod", "b.mod"])

        result = check_load_order(cfg, mods)

        assert any("依赖" in i["message"] and "b.mod" in i["message"] for i in result["issues"])

    def test_force_violation_is_an_error(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod", extra="<forceLoadAfter><li>b.mod</li></forceLoadAfter>")
        _make_mod(mods, "B", "b.mod")
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "a.mod", "b.mod"])

        result = check_load_order(cfg, mods)

        force = [i for i in result["issues"] if i.get("kind") == "forceLoadAfter"]
        assert force and force[0]["severity"] == "error"
        assert result["ok"] is False

    def test_community_rule_violation_is_reported(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "a.mod", "b.mod"])
        mods = tmp_path / "Mods"
        mods.mkdir()

        result = check_load_order(
            cfg,
            mods,
            community_rules={"a.mod": {"loadAfter": {"b.mod": {}}}},
        )

        assert any("a.mod" in i["message"] and "b.mod" in i["message"] for i in result["issues"])

    def test_user_explicit_conflict_is_an_error(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "a.mod", "b.mod"])
        mods = tmp_path / "Mods"
        mods.mkdir()

        result = check_load_order(
            cfg,
            mods,
            user_rules={"conflicts": [{"a": "a.mod", "b": "b.mod", "reason": "用户禁配"}]},
        )

        assert any("用户禁配" in i["message"] for i in result["issues"])
        assert result["ok"] is False

    def test_user_rule_overrides_community_rule(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "a.mod", "b.mod"])
        mods = tmp_path / "Mods"
        mods.mkdir()

        # Community wants a after b (violated); the user explicitly clears that
        # constraint for the same owner/field.
        result = check_load_order(
            cfg,
            mods,
            community_rules={"a.mod": {"loadAfter": {"b.mod": {}}}},
            user_rules={"rules": {"a.mod": {"loadAfter": {}}}},
        )

        assert not any("顺序相反" in i["message"] for i in result["issues"])

    def test_versioned_rule_is_selected_by_config_version(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(
            mods,
            "A",
            "a.mod",
            extra=(
                "<loadAfterByVersion><v1.5><li>b.mod</li></v1.5>"
                "<v1.6><li>c.mod</li></v1.6></loadAfterByVersion>"
            ),
        )
        _make_mod(mods, "B", "b.mod")
        _make_mod(mods, "C", "c.mod")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><version>1.6.4850 rev645</version><activeMods>"
            "<li>brrainz.harmony</li><li>a.mod</li><li>b.mod</li><li>c.mod</li>"
            "</activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        result = check_load_order(cfg, mods)

        # The v1.6 group is c.mod, and c loads after a in this config.
        assert any(
            i.get("kind") == "loadAfter" and "c.mod" in i["message"] for i in result["issues"]
        )
        # The v1.5 group (b.mod) must not be applied.
        assert not any(
            i.get("kind") == "loadAfter" and "b.mod" in i["message"] for i in result["issues"]
        )


# ── opt-in Steam Workshop database enrichment ──────────────────────

_STEAM_SAMPLE = {
    "version": 1790337009,
    "database": {
        "2009463077": {
            "packageId": "brrainz.harmony",
            "name": "Harmony",
            "gameVersions": ["1.5", "1.6"],
        },
        "111": {"packageId": "a.mod", "name": "A", "gameVersions": ["1.5"]},
        "222": {"packageId": "b.mod", "name": "B", "gameVersions": ["1.6"]},
        "333": {"name": "Workshop only", "gameVersions": ["1.6"]},
        "294100": {"packageId": "ludeon.rimworld", "name": "RimWorld"},
    },
}


def _write_steam_cache() -> None:
    cache_dir = Path.home() / ".rwmod"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "steam_db.json").write_text(json.dumps(_STEAM_SAMPLE), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_steam_db_singleton():
    from rwmod.steam_db import SteamDB

    SteamDB.reset()
    yield
    SteamDB.reset()


def _two_mod_config(tmp_path: Path) -> tuple[Path, Path]:
    mods = tmp_path / "Mods"
    mods.mkdir()
    _make_mod(mods, "A", "a.mod")
    _make_mod(mods, "B", "b.mod")
    cfg = tmp_path / "ModsConfig.xml"
    _write_modsconfig(cfg, ["brrainz.harmony", "a.mod", "b.mod"])
    return cfg, mods


class TestSteamDbEnrichment:
    def test_disabled_by_default_ignores_cache(self, tmp_path: Path):
        _write_steam_cache()
        cfg, mods = _two_mod_config(tmp_path)

        result = check_load_order(cfg, mods, rimworld_version="1.6")

        assert "steam_db" not in result

    def test_enabled_without_cache_adds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        cfg, mods = _two_mod_config(tmp_path)

        result = check_load_order(cfg, mods, rimworld_version="1.6", enable_steam_db=True)

        assert "steam_db" not in result

    def test_enabled_reports_game_versions_and_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        _write_steam_cache()
        cfg, mods = _two_mod_config(tmp_path)

        result = check_load_order(cfg, mods, rimworld_version="1.6", enable_steam_db=True)

        steam = result["steam_db"]
        assert steam["matched"] == 3
        assert steam["game_versions"]["brrainz.harmony"] == ["1.5", "1.6"]
        assert steam["game_versions"]["a.mod"] == ["1.5"]
        assert steam["game_versions"]["b.mod"] == ["1.6"]
        assert steam["unsupported"] == [{"mod": "a.mod", "game_versions": ["1.5"]}]

    def test_enabled_falls_back_to_installed_workshop_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        _write_steam_cache()
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "C", "c.mod")
        (mods / "C" / "About" / "PublishedFileId.txt").write_text("333", encoding="utf-8")
        cfg = tmp_path / "ModsConfig.xml"
        _write_modsconfig(cfg, ["brrainz.harmony", "c.mod"])

        result = check_load_order(cfg, mods, rimworld_version="1.6", enable_steam_db=True)

        assert result["steam_db"]["game_versions"]["c.mod"] == ["1.6"]
        assert result["steam_db"]["unsupported"] == []

    def test_corrupt_cache_is_fail_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import steam_db as sd_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        cache_dir = Path.home() / ".rwmod"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "steam_db.json").write_text("{broken", encoding="utf-8")
        cfg, mods = _two_mod_config(tmp_path)

        result = check_load_order(cfg, mods, rimworld_version="1.6", enable_steam_db=True)

        assert "steam_db" not in result

    def test_unknown_mods_are_not_counted(self, tmp_path: Path):
        _write_steam_cache()
        cfg, mods = _two_mod_config(tmp_path)
        cfg.write_text(
            "<ModsConfigData><version>1.6.4850 rev645</version><activeMods>"
            "<li>brrainz.harmony</li><li>a.mod</li><li>b.mod</li>"
            "<li>ghost.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        result = check_load_order(
            cfg,
            mods,
            rimworld_version="1.6",
            enable_steam_db=True,
            community_rules={},
            user_rules={},
        )

        assert result["steam_db"]["matched"] == 3
        assert set(result["steam_db"]["game_versions"]) == {
            "brrainz.harmony",
            "a.mod",
            "b.mod",
        }
        assert "ghost.mod" not in result["steam_db"]["game_versions"]
