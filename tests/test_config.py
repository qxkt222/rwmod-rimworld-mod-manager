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


class TestUpdateAndSourceDefaults:
    """P1-2 / P2-2: new fields default sanely and survive a round-trip."""

    def test_defaults(self):
        cfg = Config()
        assert cfg.update_backup is True
        assert cfg.backup_keep == 3
        assert cfg.enable_skymods is False

    def test_load_reads_new_fields(self, tmp_path: Path):
        f = tmp_path / ".rwmod.toml"
        f.write_text(
            'steamcmd_path = "D:/steamcmd/steamcmd.exe"\n'
            'mods_dir = "E:/MyMods"\n'
            "update_backup = false\n"
            "backup_keep = 5\n"
            "enable_skymods = true\n"
        )
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config.load()
        assert cfg.update_backup is False
        assert cfg.backup_keep == 5
        assert cfg.enable_skymods is True

    def test_old_config_without_new_fields_uses_defaults(self, tmp_path: Path):
        f = tmp_path / ".rwmod.toml"
        f.write_text(
            'steamcmd_path = "D:/steamcmd/steamcmd.exe"\n'
            'mods_dir = "E:/MyMods"\n'
            'rimworld_dir = "E:/RimWorld"\n'
        )
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config.load()
        assert cfg.update_backup is True
        assert cfg.backup_keep == 3
        assert cfg.enable_skymods is False

    def test_save_round_trips_new_fields(self, tmp_path: Path):
        f = tmp_path / "roundtrip.toml"
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config(mods_dir=Path("Z:/Test"))
            cfg.update_backup = False
            cfg.backup_keep = 7
            cfg.enable_skymods = True
            cfg.save()
            loaded = Config.load()
        assert loaded.update_backup is False
        assert loaded.backup_keep == 7
        assert loaded.enable_skymods is True


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


class TestStatusSources:
    """P2-2: /api/status exposes the fallback source state as a new field."""

    def test_status_includes_sources_skymods(self, tmp_path: Path):
        from rwmod.routers.health import api_status

        f = tmp_path / "status.toml"
        f.write_text("")
        with patch.object(Config, "CONFIG_PATH", f):
            body = api_status()
        assert body["sources"]["skymods"] == "disabled"
        # The pre-existing connectivity fields are untouched.
        assert "online" in body


class TestConfigSave:
    def test_save_and_reload(self, tmp_path: Path):
        f = tmp_path / "save_test.toml"
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config(mods_dir=Path("Z:/Test"))
            cfg.save()
            assert f.exists()
            content = f.read_text(encoding="utf-8")
            assert 'mods_dir = "Z:/Test"' in content


class TestInstances:
    """Wave 6a — config v2: [[instances]] + active_instance, old flat files upgrade."""

    def test_old_flat_config_synthesizes_default_instance(self, tmp_path: Path):
        f = tmp_path / "old.toml"
        f.write_text(
            'steamcmd_path = "D:/steamcmd/steamcmd.exe"\n'
            'mods_dir = "E:/MyMods"\n'
            'rimworld_dir = "E:/RimWorld"\n'
            'backup_dir = "F:/Backups"\n'
            'config_dir = "G:/Config"\n'
        )
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config.load()
        assert cfg.active_instance == "default"
        assert [i.id for i in cfg.instances] == ["default"]
        assert cfg.instances[0].name
        assert cfg.mods_dir == Path("E:/MyMods")
        assert cfg.rimworld_dir == Path("E:/RimWorld")
        assert cfg.backup_dir == Path("F:/Backups")
        assert cfg.config_dir == Path("G:/Config")

    def test_save_writes_instances_and_flat_mirror(self, tmp_path: Path):
        import tomllib

        f = tmp_path / "v2.toml"
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config(mods_dir=Path("A:/Mods"), rimworld_dir=Path("A:/RimWorld"))
            second = cfg.add_instance(
                name="Second",
                mods_dir=Path("B:/Mods"),
                rimworld_dir=Path("B:/RimWorld"),
            )
            cfg.activate_instance(second.id)
            cfg.save()
            text = f.read_text(encoding="utf-8")
            assert "[[instances]]" in text
            data = tomllib.loads(text)

            assert data["active_instance"] == second.id
            assert [i["id"] for i in data["instances"]] == ["default", second.id]
            # Flat mirror keys describe the *active* instance so an older rwmod
            # build keeps operating on the same paths.
            assert data["mods_dir"] == "B:/Mods"
            assert data["rimworld_dir"] == "B:/RimWorld"

            loaded = Config.load()
        assert loaded.active_instance == second.id
        assert loaded.mods_dir == Path("B:/Mods")
        assert loaded.rimworld_dir == Path("B:/RimWorld")
        assert loaded.get_instance("default").mods_dir == Path("A:/Mods")

    def test_flat_properties_follow_the_active_instance(self, tmp_path: Path):
        cfg = Config(mods_dir=Path("A:/Mods"), rimworld_dir=Path("A:/RW"))
        second = cfg.add_instance(
            name="Second", mods_dir=Path("B:/Mods"), rimworld_dir=Path("B:/RW")
        )
        assert cfg.mods_dir == Path("A:/Mods")

        cfg.activate_instance(second.id)
        assert cfg.mods_dir == Path("B:/Mods")
        cfg.mods_dir = Path("C:/Mods")  # setter writes to the active instance only
        assert cfg.get_instance(second.id).mods_dir == Path("C:/Mods")
        assert cfg.get_instance("default").mods_dir == Path("A:/Mods")

    def test_unknown_active_instance_falls_back_to_first(self, tmp_path: Path):
        f = tmp_path / "bad-active.toml"
        f.write_text(
            'active_instance = "ghost"\n'
            "[[instances]]\n"
            'id = "alpha"\n'
            'name = "Alpha"\n'
            'mods_dir = "A:/Mods"\n'
            'rimworld_dir = "A:/RW"\n'
            "[[instances]]\n"
            'id = "beta"\n'
            'name = "Beta"\n'
            'mods_dir = "B:/Mods"\n'
            'rimworld_dir = "B:/RW"\n'
        )
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config.load()
        assert cfg.active_instance == "alpha"
        assert cfg.mods_dir == Path("A:/Mods")

    def test_duplicate_and_unsafe_ids_are_rejected(self):
        cfg = Config(mods_dir=Path("A:/Mods"))
        cfg.add_instance(instance_id="alpha", name="Alpha", mods_dir=Path("B:/Mods"))
        with pytest.raises(ValueError):
            cfg.add_instance(instance_id="alpha", name="Again", mods_dir=Path("C:/Mods"))
        for bad in ("../evil", "a/b", "a\\b", "", ".", "..", "-leading", "x" * 65):
            with pytest.raises(ValueError):
                cfg.add_instance(instance_id=bad, name="Bad", mods_dir=Path("C:/Mods"))

    def test_generated_ids_are_unique_and_path_safe(self):
        cfg = Config(mods_dir=Path("A:/Mods"))
        a = cfg.add_instance(name="我的 实例", mods_dir=Path("B:/Mods"))
        b = cfg.add_instance(name="我的 实例", mods_dir=Path("C:/Mods"))
        assert a.id != b.id
        for iid in (a.id, b.id):
            assert iid.isascii()
            assert "/" not in iid and "\\" not in iid and iid not in (".", "..")

    def test_remove_instance_rules(self):
        cfg = Config(mods_dir=Path("A:/Mods"))
        with pytest.raises(ValueError):
            cfg.remove_instance("default")  # active and last
        second = cfg.add_instance(name="Second", mods_dir=Path("B:/Mods"))
        cfg.remove_instance(second.id)  # non-active → allowed
        assert [i.id for i in cfg.instances] == ["default"]
        with pytest.raises(KeyError):
            cfg.remove_instance("missing")

    def test_activate_unknown_raises(self):
        cfg = Config(mods_dir=Path("A:/Mods"))
        with pytest.raises(KeyError):
            cfg.activate_instance("nope")

    def test_save_invalidates_the_load_cache(self, tmp_path: Path):
        f = tmp_path / "cache.toml"
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config(mods_dir=Path("A:/Mods"))
            second = cfg.add_instance(name="Second", mods_dir=Path("B:/Mods"))
            cfg.activate_instance(second.id)
            cfg.save()
            assert Config.load().active_instance == second.id

    def test_instances_do_not_share_mutable_paths(self):
        cfg = Config(mods_dir=Path("A:/Mods"), config_dir=Path("A:/Config"))
        second = cfg.add_instance(name="Second", mods_dir=Path("B:/Mods"))
        assert second.config_dir is None
        assert cfg.get_instance("default").config_dir == Path("A:/Config")


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
