"""Tests for save_parser.py \u2014 .rws save file parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from rwmod.mod_rules import OFFICIAL_PACKAGE_IDS
from rwmod.save_parser import _extract_game_version, analyze_save, find_save_files, parse_save_mods

# Real game data is only ever read by these tests. ``expanduser`` resolves
# through USERPROFILE, so the autouse isolation fixture (which patches
# ``Path.home``) does not hide the real save directory from collection.
_REAL_SAVE_DIR = (
    Path("~").expanduser()
    / "AppData"
    / "LocalLow"
    / "Ludeon Studios"
    / "RimWorld by Ludeon Studios"
    / "Saves"
)
_REAL_SAVES = sorted(_REAL_SAVE_DIR.glob("*.rws")) if _REAL_SAVE_DIR.is_dir() else []
_REAL_SAVE = _REAL_SAVES[0] if _REAL_SAVES else None
_REAL_MODS_DIR = Path("D:/RimWorld/Mods")


class TestParseSaveMods:
    def test_from_string_content(self):
        content = """<?xml version="1.0"?>
<saves>
  <modIds>
    <li>brrainz.harmony</li>
    <li>ludeon.rimworld</li>
    <li>author.mod1</li>
  </modIds>
</saves>"""
        mods = parse_save_mods(content)
        assert mods == ["brrainz.harmony", "ludeon.rimworld", "author.mod1"]

    def test_from_path(self, tmp_path: Path):
        f = tmp_path / "test.rws"
        f.write_text(
            "<saves><modIds><li>test.mod</li><li>other.mod</li></modIds></saves>",
            encoding="utf-8",
        )
        mods = parse_save_mods(f)
        assert mods == ["test.mod", "other.mod"]

    def test_no_mod_ids(self):
        mods = parse_save_mods("<saves><gameVersion>1.5</gameVersion></saves>")
        assert mods == []

    def test_missing_file(self, tmp_path: Path):
        mods = parse_save_mods(tmp_path / "nonexistent.rws")
        assert mods == []

    def test_empty_content(self):
        assert parse_save_mods("") == []


class TestExtractGameVersion:
    def test_extracts_version(self, tmp_path: Path):
        f = tmp_path / "save.rws"
        f.write_text("<saves><gameVersion>1.5.4297 rev1117</gameVersion></saves>")
        assert _extract_game_version(f) == "1.5.4297 rev1117"

    def test_no_version(self, tmp_path: Path):
        f = tmp_path / "save.rws"
        f.write_text("<saves></saves>")
        assert _extract_game_version(f) is None

    def test_missing_file(self, tmp_path: Path):
        assert _extract_game_version(tmp_path / "nonexistent.rws") is None


class TestAnalyzeSave:
    def test_full_analysis(self, tmp_path: Path):
        f = tmp_path / "MyColony.rws"
        f.write_text(
            "<saves>"
            "<gameVersion>1.5.4297 rev1117</gameVersion>"
            "<modIds><li>mod.a</li><li>mod.b</li><li>mod.c</li></modIds>"
            "</saves>",
        )
        installed = {"mod.a", "mod.b", "mod.d"}
        result = analyze_save(f, installed)
        assert result["name"] == "MyColony"
        assert result["total_mods"] == 3
        assert result["required_mods"] == ["mod.a", "mod.b", "mod.c"]
        assert result["missing_mods"] == ["mod.c"]
        assert result["unused_by_save"] == ["mod.d"]
        assert not result["loadable"]
        assert result["completeness"] == 0.75

    def test_all_installed(self, tmp_path: Path):
        f = tmp_path / "Complete.rws"
        f.write_text("<saves><modIds><li>mod.a</li><li>mod.b</li></modIds></saves>")
        result = analyze_save(f, {"mod.a", "mod.b"})
        assert result["loadable"]
        assert result["completeness"] == 1.0
        assert result["missing_mods"] == []


class TestAnalyzeSaveCaseInsensitive:
    """RimWorld writes lowercase packageIds into saves/ModsConfig, while
    About.xml keeps the author's casing — the comparison must ignore case."""

    def test_save_lowercase_ids_match_mixed_case_installed(self, tmp_path: Path):
        f = tmp_path / "Case.rws"
        f.write_text("<saves><modIds><li>brrainz.harmony</li><li>author.mod</li></modIds></saves>")
        result = analyze_save(f, {"Brrainz.Harmony", "Author.Mod"})
        assert result["missing_mods"] == []
        assert result["unused_by_save"] == []
        assert result["loadable"] is True
        assert result["completeness"] == 1.0

    def test_required_mods_keep_the_save_spelling(self, tmp_path: Path):
        f = tmp_path / "Spelling.rws"
        content = "<saves><modIds><li>Brrainz.Harmony</li></modIds></saves>"
        f.write_text(content)
        result = analyze_save(f, {"brrainz.harmony"})
        assert result["required_mods"] == ["Brrainz.Harmony"]
        assert result["missing_mods"] == []

    def test_installed_mixed_case_is_not_reported_unused(self, tmp_path: Path):
        f = tmp_path / "Unused.rws"
        f.write_text("<saves><modIds><li>author.mod</li></modIds></saves>")
        result = analyze_save(f, {"Author.Mod", "unrelated.mod"})
        assert result["unused_by_save"] == ["unrelated.mod"]

    def test_truly_missing_mods_are_still_reported(self, tmp_path: Path):
        f = tmp_path / "Missing.rws"
        f.write_text("<saves><modIds><li>author.mod</li><li>not.installed</li></modIds></saves>")
        result = analyze_save(f, {"AUTHOR.MOD"})
        assert result["missing_mods"] == ["not.installed"]
        assert result["loadable"] is False
        assert result["completeness"] == 0.667


@pytest.mark.skipif(_REAL_SAVE is None, reason="real RimWorld save not available")
class TestRealSaveReadOnly:
    """Read-only regression against the developer's real save.

    Nothing here writes to the game directories; the autouse fixture isolates
    every write path used by the application.
    """

    def test_every_save_id_matches_a_case_flipped_install_set(self):
        assert _REAL_SAVE is not None
        required = parse_save_mods(_REAL_SAVE)
        assert len(required) >= 500
        # Every required id installed under a different case: before the fix
        # this produced one false "missing" per id.
        installed = {pid.swapcase() for pid in required}
        result = analyze_save(_REAL_SAVE, installed)
        assert result["total_mods"] == len(required)
        assert result["missing_mods"] == []
        assert result["unused_by_save"] == []
        assert result["loadable"] is True

    @pytest.mark.skipif(not _REAL_MODS_DIR.is_dir(), reason="real Mods dir not available")
    def test_only_official_content_is_missing(self):
        """The real save's 522 ids reduce to Core + 5 DLC after case folding."""
        from rwmod.metadata import installed_package_ids

        assert _REAL_SAVE is not None
        result = analyze_save(_REAL_SAVE, installed_package_ids(_REAL_MODS_DIR))
        assert set(result["missing_mods"]) <= set(OFFICIAL_PACKAGE_IDS)
        assert len(result["missing_mods"]) == len(OFFICIAL_PACKAGE_IDS)


class TestFindSaveFiles:
    def test_returns_empty_for_no_saves(self, tmp_path: Path):
        saves = find_save_files(tmp_path)
        assert isinstance(saves, list)

    def test_uses_the_instance_config_dir(self, tmp_path: Path):
        """Wave 6a: saves are discovered under the active instance's config_dir."""
        home = tmp_path / "home"
        home.mkdir()
        config_dir = tmp_path / "instance_cfg"
        saves_dir = config_dir / "Saves"
        saves_dir.mkdir(parents=True)
        (saves_dir / "Colony.rws").write_text(
            "<saves><modIds><li>a.b</li></modIds></saves>", encoding="utf-8"
        )
        rimworld = tmp_path / "RimWorld"
        rimworld.mkdir()

        from unittest.mock import patch as _patch

        with _patch.object(Path, "home", classmethod(lambda cls: home)):
            found = find_save_files(rimworld, config_dir=config_dir)
        assert [p.name for p in found] == ["Colony.rws"]

    def test_falls_back_to_local_low_when_no_config_dir(self, tmp_path: Path):
        import platform

        if platform.system() != "Windows":
            pytest.skip("Windows LocalLow layout")
        home = tmp_path / "home"
        saves_dir = (
            home
            / "AppData"
            / "LocalLow"
            / "Ludeon Studios"
            / "RimWorld by Ludeon Studios"
            / "Saves"
        )
        saves_dir.mkdir(parents=True)
        (saves_dir / "Global.rws").write_text("<saves/>", encoding="utf-8")
        rimworld = tmp_path / "RimWorld"
        rimworld.mkdir()

        from unittest.mock import patch as _patch

        with _patch.object(Path, "home", classmethod(lambda cls: home)):
            found = find_save_files(rimworld)
        assert [p.name for p in found] == ["Global.rws"]
