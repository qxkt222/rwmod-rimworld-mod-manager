"""Test config management: load, save, auto-migrate to built-in SteamCMD."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.config import Config


class TestConfigDefaults:
    def test_default_steamcmd_exists(self):
        # When built-in steamcmd.exe exists, it should be the default
        with patch.object(Path, "exists", return_value=True):
            cfg = Config()
            assert "steamcmd" in str(cfg.steamcmd_path).lower()

    def test_fallback_when_builtin_missing(self):
        with patch.object(Path, "exists", return_value=False):
            # The fallback is D:/steamcmd/steamcmd.exe (returned by _default_steamcmd_path)
            import rwmod.config as cfg_module

            with patch.object(
                cfg_module.Config,
                "_default_steamcmd_path",
                return_value=Path("D:/steamcmd/steamcmd.exe"),
            ):
                c = Config()
                assert str(c.steamcmd_path).replace("\\", "/") == "D:/steamcmd/steamcmd.exe"

    def test_backup_dir_defaults_to_mods_subdir(self):
        cfg = Config(mods_dir=Path("D:/RimWorld/Mods"))
        assert cfg.backup_dir == Path("D:/RimWorld/Mods/_backups")

    def test_explicit_backup_dir(self):
        cfg = Config(backup_dir=Path("E:/Backups"))
        assert cfg.backup_dir == Path("E:/Backups")


class TestConfigLoad:
    def test_load_nonexistent_creates_defaults(self, tmp_path: Path):
        with patch.object(Config, "CONFIG_PATH", tmp_path / "nonexistent.toml"):
            cfg = Config.load()
            assert cfg.mods_dir == Path("D:/RimWorld/Mods")

    def test_load_existing(self, tmp_path: Path):
        f = tmp_path / ".rwmod.toml"
        f.write_text(
            'steamcmd_path = "D:/steamcmd/steamcmd.exe"\n'
            'mods_dir = "E:/MyMods"\n'
            'rimworld_dir = "E:/RimWorld"\n'
            'backup_dir = "F:/Backups"\n'
        )
        with patch.object(Config, "CONFIG_PATH", f):
            with patch.object(
                Path, "exists", lambda self: self != Path("D:/steamcmd/steamcmd.exe")
            ):
                cfg = Config.load()
                assert cfg.mods_dir == Path("E:/MyMods")
                assert cfg.backup_dir == Path("F:/Backups")

    def test_respects_user_configured_steamcmd(self, tmp_path: Path):
        """A valid user-configured steamcmd_path must be honored — the old code
        silently overrode it whenever the bundled copy existed, making the
        config panel's steamcmd field appear to do nothing."""
        f = tmp_path / ".rwmod.toml"
        f.write_text(
            'steamcmd_path = "D:/user/steamcmd/steamcmd.exe"\n'
            'mods_dir = "D:/RimWorld/Mods"\n'
            'rimworld_dir = "D:/RimWorld"\n'
        )
        with patch.object(Config, "CONFIG_PATH", f):
            # Both the builtin and the user path exist → user wins.
            with patch.object(Path, "exists", return_value=True):
                cfg = Config.load()
                assert str(cfg.steamcmd_path).replace("\\", "/") == "D:/user/steamcmd/steamcmd.exe"

    def test_falls_back_to_builtin_when_configured_broken(self, tmp_path: Path):
        """A stale configured path (file deleted) falls back to the bundled
        SteamCMD instead of failing validation."""
        f = tmp_path / ".rwmod.toml"
        f.write_text(
            'steamcmd_path = "D:/gone/steamcmd.exe"\n'
            'mods_dir = "D:/RimWorld/Mods"\n'
            'rimworld_dir = "D:/RimWorld"\n'
        )
        with patch.object(Config, "CONFIG_PATH", f):
            # Builtin exists, user path does not → builtin.
            with patch.object(Path, "exists", lambda self: "gone" not in str(self)):
                cfg = Config.load()
                assert "steamcmd" in str(cfg.steamcmd_path).lower()
                assert "gone" not in str(cfg.steamcmd_path)


class TestConfigSave:
    def test_save_and_reload(self, tmp_path: Path):
        f = tmp_path / "save_test.toml"
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config(mods_dir=Path("Z:/Test"))
            cfg.save()
            assert f.exists()
            content = f.read_text()
            assert 'mods_dir = "Z:/Test"' in content


class TestConfigValidate:
    def test_requires_steamcmd_only(self, tmp_path: Path):
        """RimWorld game dir is optional — only SteamCMD is required."""
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe")
        (tmp_path / "steamcmd.exe").touch()
        cfg.mods_dir = tmp_path / "Mods"
        cfg.rimworld_dir = tmp_path / "NonexistentGameDir"  # missing — must not fail
        cfg.validate()
        assert cfg.mods_dir.exists()

    def test_missing_steamcmd_raises(self, tmp_path: Path):
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe")  # not touched
        cfg.mods_dir = tmp_path / "Mods"
        try:
            cfg.validate()
        except Exception as exc:
            assert "SteamCMD" in str(exc)
        else:
            pytest.fail("validate() should raise when SteamCMD is missing")

    def test_creates_mods_dir(self, tmp_path: Path):
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe")
        (tmp_path / "steamcmd.exe").touch()
        cfg.mods_dir = tmp_path / "Created" / "Mods"
        cfg.validate()
        assert cfg.mods_dir.exists()
