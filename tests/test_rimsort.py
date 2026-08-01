"""Tests for rimsort.py — ModsConfig.xml generation, parsing, comparison."""

from __future__ import annotations

from pathlib import Path

from rwmod.rimsort import (
    compare_modsconfig,
    generate_modsconfig,
    parse_modsconfig,
    resolve_missing_workshop_ids,
)


def _make_mod(mods_dir: Path, folder: str, package_id: str, workshop_id: str = "") -> None:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><packageId>{package_id}</packageId></ModMetaData>", encoding="utf-8"
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
