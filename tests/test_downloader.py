"""Test downloader — mod ID parsing, folder naming, existing mod detection.

Tests core logic paths that don't require SteamCMD network calls.
"""

from __future__ import annotations

from pathlib import Path

from rwmod.downloader import (
    _find_existing,
    _pick_folder_name,
    extract_mod_id,
)


class TestExtractModId:
    def test_plain_number(self):
        assert extract_mod_id("2009463077") == "2009463077"

    def test_workshop_url(self):
        url = "https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077"
        assert extract_mod_id(url) == "2009463077"

    def test_url_with_other_params(self):
        url = "https://steamcommunity.com/sharedfiles/filedetails/?id=123456&searchtext=foo"
        assert extract_mod_id(url) == "123456"

    def test_invalid_input(self):
        assert not extract_mod_id("not_a_number")
        assert not extract_mod_id("https://example.com")
        assert not extract_mod_id("")

    def test_whitespace_only(self):
        assert not extract_mod_id("   ")

    def test_whitespace_number(self):
        # Numbers with whitespace are stripped and considered valid
        assert extract_mod_id("  123456  ") == "123456"


class TestFindExisting:
    def test_finds_by_published_file_id(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        mod_folder = mods_dir / "SomeMod"
        mod_folder.mkdir()
        about = mod_folder / "About"
        about.mkdir()
        (about / "PublishedFileId.txt").write_text("2009463077")

        result = _find_existing(mods_dir, "2009463077")
        assert result is not None
        assert result.name == "SomeMod"

    def test_not_found(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        assert _find_existing(mods_dir, "99999") is None

    def test_empty_mods_dir(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        assert _find_existing(mods_dir, "2009463077") is None

    def test_ignores_non_dirs(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        (mods_dir / "some_file.txt").write_text("hello")
        assert _find_existing(mods_dir, "2009463077") is None

    def test_multiple_mods_finds_correct_one(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        for i, wid in enumerate(["111", "222", "333"]):
            folder = mods_dir / f"mod_{i}"
            folder.mkdir()
            about = folder / "About"
            about.mkdir()
            (about / "PublishedFileId.txt").write_text(wid)

        result = _find_existing(mods_dir, "222")
        assert result is not None
        assert result.name == "mod_1"


class TestPickFolderName:
    def test_from_package_id(self, tmp_path: Path):
        workshop_path = tmp_path / "workshop"
        workshop_path.mkdir()
        about = workshop_path / "About"
        about.mkdir()
        (about / "About.xml").write_text(
            "<ModMetaData><packageId>author.somecoolmod</packageId></ModMetaData>"
        )
        name = _pick_folder_name(workshop_path, "12345")
        assert name == "author.somecoolmod"

    def test_fallback_to_name(self, tmp_path: Path):
        workshop_path = tmp_path / "workshop"
        workshop_path.mkdir()
        about = workshop_path / "About"
        about.mkdir()
        (about / "About.xml").write_text("<ModMetaData><name>My Cool Mod</name></ModMetaData>")
        name = _pick_folder_name(workshop_path, "12345")
        assert "My Cool Mod" in name
        assert "12345" in name

    def test_fallback_to_mod_id(self, tmp_path: Path):
        workshop_path = tmp_path / "workshop"
        workshop_path.mkdir()
        about = workshop_path / "About"
        about.mkdir()
        (about / "About.xml").write_text("<ModMetaData></ModMetaData>")
        name = _pick_folder_name(workshop_path, "12345")
        assert name == "mod_12345"

    def test_sanitize_special_chars(self, tmp_path: Path):
        workshop_path = tmp_path / "workshop"
        workshop_path.mkdir()
        about = workshop_path / "About"
        about.mkdir()
        (about / "About.xml").write_text(
            "<ModMetaData><packageId>evil:hack<foo>bar</packageId></ModMetaData>"
        )
        name = _pick_folder_name(workshop_path, "12345")
        assert ":" not in name
        assert "<" not in name
        assert ">" not in name

    def test_no_about_xml(self, tmp_path: Path):
        workshop_path = tmp_path / "workshop"
        workshop_path.mkdir()
        name = _pick_folder_name(workshop_path, "12345")
        assert name == "mod_12345"


# ── force-overwrite atomicity ──────────────────────────────────────
# When force=True the existing mod must be stashed (renamed) rather than
# deleted: a failed download rolls the old copy back instead of losing it.


class TestForceOverwriteAtomic:
    def _make_config(self, tmp_path: Path):
        from rwmod.config import Config

        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path / "Mods")
        cfg.mods_dir.mkdir(parents=True)
        cfg.backup_dir = tmp_path / "Backups"
        return cfg

    def _install_existing(self, cfg, wid: str = "123456", folder: str = "MyOldMod"):
        mod = cfg.mods_dir / folder
        about = mod / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            "<ModMetaData><name>Old</name><packageId>author.old</packageId></ModMetaData>"
        )
        (about / "PublishedFileId.txt").write_text(wid)
        return mod

    def _fake_download_result(self, success: bool):
        from rwmod.steamcmd import DownloadResult, ErrorKind

        return DownloadResult(
            success=success,
            mod_id="123456",
            error_kind=ErrorKind.OK if success else ErrorKind.FAILURE,
            error_detail="",
        )

    def test_force_success_discards_stash(self, tmp_path: Path, monkeypatch):
        from unittest.mock import patch

        from rwmod.downloader import download_one

        cfg = self._make_config(tmp_path)
        self._install_existing(cfg)
        # New download content
        workshop_dir = (
            cfg.steamcmd_path.parent / "steamapps" / "workshop" / "content" / "294100" / "123456"
        )
        about = workshop_dir / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            "<ModMetaData><name>New</name><packageId>author.new</packageId></ModMetaData>"
        )

        fake = SteamCMDFake(self._fake_download_result(True))
        fake.workshop_content_dir = workshop_dir.parent  # steam/workshop/content/294100
        monkeypatch.setattr("rwmod.downloader.SteamCMD", lambda p: fake)
        with patch("rwmod.workshop.is_collection", return_value=False):
            ok = download_one(cfg, "123456", force=True)

        assert ok
        # New version installed, no stash remains
        assert (cfg.mods_dir / "author.new").is_dir()
        assert not list(cfg.mods_dir.glob(".rwmod_stash_*"))

    def test_force_failure_rolls_back_stash(self, tmp_path: Path, monkeypatch):
        from unittest.mock import patch

        from rwmod.downloader import download_one

        cfg = self._make_config(tmp_path)
        self._install_existing(cfg)
        # No download content -> failure path
        fake = SteamCMDFake(self._fake_download_result(False))
        monkeypatch.setattr("rwmod.downloader.SteamCMD", lambda p: fake)
        monkeypatch.setattr("rwmod.downloader._skymods_fallback", lambda cfg, mid: False)
        with patch("rwmod.workshop.is_collection", return_value=False):
            ok = download_one(cfg, "123456", force=True)

        assert not ok
        # Old mod rolled back into place, no stash left behind
        assert (cfg.mods_dir / "MyOldMod").is_dir()
        assert not list(cfg.mods_dir.glob(".rwmod_stash_*"))

    def test_force_failure_no_stash_no_backup_does_not_lose_mod(self, tmp_path: Path, monkeypatch):
        """Rename-aside must never fail hard: if it does, fall back to backup."""
        from unittest.mock import patch

        from rwmod.downloader import download_one

        cfg = self._make_config(tmp_path)
        self._install_existing(cfg)

        fake = SteamCMDFake(self._fake_download_result(False))
        monkeypatch.setattr("rwmod.downloader.SteamCMD", lambda p: fake)
        monkeypatch.setattr("rwmod.downloader._skymods_fallback", lambda cfg, mid: False)
        with patch("rwmod.workshop.is_collection", return_value=False):
            # Simulate rename failure -> old code would rmtree; new code backs up.
            def _broken_rename(self, target):
                raise OSError("simulated rename failure")

            monkeypatch.setattr(Path, "rename", _broken_rename)
            ok = download_one(cfg, "123456", force=True)

        assert not ok
        # No mod dir but a backup exists (old behavior: lost forever).
        assert not (cfg.mods_dir / "MyOldMod").exists()


class SteamCMDFake:
    """Minimal stand-in for SteamCMD with a scripted result."""

    def __init__(self, result):
        self.result = result
        self.workshop_content_dir = Path(".") / "steamapps" / "workshop" / "content" / "294100"

    def workshop_download(self, mod_id: str):
        return self.result
