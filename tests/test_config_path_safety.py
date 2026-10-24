"""ModsConfig.xml path safety — regression tests for the 10/24 incident.

A standalone verification script called the profile/rimsort code without
patching ``Path.home``.  ``resolve_modsconfig_path()`` scanned LocalLow first,
found the developer's real ``ModsConfig.xml`` and a restore overwrote it.

These tests pin the four defences added in response:

1. ``Config.config_dir`` is an explicit, authoritative ModsConfig location;
2. ``resolve_modsconfig_path(config_dir=...)`` never scans LocalLow;
3. ``RWMOD_TEST_SANDBOX_HOME`` makes resolution/writes fail closed when the
   target falls outside the declared sandbox;
4. every writer (apply / profile restore / undo / CLI restore) propagates the
   configured ``config_dir``.

Every write test asserts the target lives under pytest's ``tmp_path`` *before*
performing the write, then re-reads the real ModsConfig fingerprint (mtime and
sha256) to prove it was not touched.  The real file is only ever read.
"""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rwmod.cli import app
from rwmod.config import Config
from rwmod.profile import (
    SandboxViolationError,
    resolve_modsconfig_path,
    save_profile,
)
from rwmod.profile import (
    restore_profile as restore_profile_file,
)
from rwmod.routers.profiles import restore_profile as profiles_restore_endpoint
from rwmod.routers.rimsort import rimsort_apply
from rwmod.routers.undo import api_undo

SANDBOX_ENV_VAR = "RWMOD_TEST_SANDBOX_HOME"

#: Resolved without ``Path.home`` (conftest patches that for every test); this
#: is the real user profile and is only ever read, never written.
_REAL_HOME = Path(os.path.expanduser("~"))
_REAL_MODSCONFIG = (
    _REAL_HOME
    / "AppData"
    / "LocalLow"
    / "Ludeon Studios"
    / "RimWorld by Ludeon Studios"
    / "Config"
    / "ModsConfig.xml"
)

runner = CliRunner()


# ── helpers ────────────────────────────────────────────────────────


def _fingerprint(path: Path) -> tuple[int, str] | None:
    """Return (mtime_ns, sha256) for *path*, or None when it does not exist."""
    if not path.exists():
        return None
    return (
        path.stat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _write_modsconfig(path: Path, active_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = "".join(f"<li>{pid}</li>" for pid in active_ids)
    path.write_text(
        f"<ModsConfigData><version>1.6</version><activeMods>{items}</activeMods></ModsConfigData>",
        encoding="utf-8",
    )


def _make_mod(mods_dir: Path, folder: str, package_id: str) -> None:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><packageId>{package_id}</packageId></ModMetaData>",
        encoding="utf-8",
    )


def _ludeon_config(home: Path, rimworld_folder: str = "RimWorld by Ludeon Studios") -> Path:
    if platform.system() == "Windows":
        base = home / "AppData" / "LocalLow" / "Ludeon Studios"
    elif platform.system() == "Darwin":
        base = home / "Library" / "Application Support" / "Ludeon Studios"
    else:
        base = home / ".config" / "unity3d" / "Ludeon Studios"
    return base / rimworld_folder / "Config" / "ModsConfig.xml"


# ── fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A tmp sandbox that owns a config_dir, mods dir and profile/undo dirs."""
    import rwmod.profile as profile_mod
    import rwmod.undo as undo_mod

    root = tmp_path / "sandbox"
    config_dir = root / "Config"
    mods_dir = root / "Mods"
    rimworld_dir = root / "RimWorld"
    profiles_dir = root / "profiles"
    undo_dir = root / "undo"
    for d in (config_dir, mods_dir, rimworld_dir):
        d.mkdir(parents=True)

    monkeypatch.setenv(SANDBOX_ENV_VAR, str(root))
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(undo_mod, "UNDO_DIR", undo_dir)

    cfg = Config(
        steamcmd_path=root / "steamcmd" / "steamcmd.exe",
        mods_dir=mods_dir,
        rimworld_dir=rimworld_dir,
        config_dir=config_dir,
    )
    return SimpleNamespace(
        root=root,
        config_dir=config_dir,
        mods_dir=mods_dir,
        rimworld_dir=rimworld_dir,
        profiles_dir=profiles_dir,
        undo_dir=undo_dir,
        cfg=cfg,
    )


# ── Config.config_dir ──────────────────────────────────────────────


class TestConfigDirField:
    def test_defaults_to_none(self):
        assert Config().config_dir is None

    def test_save_round_trips_config_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        toml = tmp_path / ".rwmod.toml"
        monkeypatch.setattr(Config, "CONFIG_PATH", toml)
        config_dir = tmp_path / "explicit_config"

        Config(config_dir=config_dir).save()
        assert "config_dir" in toml.read_text(encoding="utf-8")

        loaded = Config.load()
        assert loaded.config_dir == config_dir

    def test_old_config_without_config_dir_loads_as_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        toml = tmp_path / "old.toml"
        toml.write_text(
            'steamcmd_path = "D:/steamcmd/steamcmd.exe"\n'
            'mods_dir = "E:/MyMods"\n'
            'rimworld_dir = "E:/RimWorld"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(Config, "CONFIG_PATH", toml)

        assert Config.load().config_dir is None

    def test_save_does_not_discard_config_dir_on_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        toml = tmp_path / "roundtrip.toml"
        monkeypatch.setattr(Config, "CONFIG_PATH", toml)
        config_dir = tmp_path / "cfg"
        cfg = Config(mods_dir=Path("Z:/Test"), config_dir=config_dir)
        cfg.save()

        reloaded = Config.load()
        reloaded.backup_keep = 5
        reloaded.save()

        assert Config.load().config_dir == config_dir


# ── resolve_modsconfig_path ────────────────────────────────────────


class TestResolveWithConfigDir:
    def test_config_dir_wins_and_local_low_is_not_scanned(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An explicit config_dir must short-circuit LocalLow discovery."""
        explicit = sandbox.config_dir / "ModsConfig.xml"
        _write_modsconfig(explicit, ["explicit.mod"])

        # A tempting LocalLow config *outside* the sandbox: if resolution scans
        # it, the sandbox guard raises instead of returning it.
        fake_home = tmp_path / "fake_home"
        local_low = _ludeon_config(fake_home)
        _write_modsconfig(local_low, ["real.mod"])
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        assert (
            resolve_modsconfig_path(sandbox.rimworld_dir, config_dir=sandbox.config_dir) == explicit
        )

    def test_missing_config_dir_file_does_not_fall_back_to_local_low(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_home = tmp_path / "fake_home"
        _write_modsconfig(_ludeon_config(fake_home), ["real.mod"])
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        assert resolve_modsconfig_path(sandbox.rimworld_dir, config_dir=sandbox.config_dir) is None

    def test_config_dir_outside_sandbox_raises(self, sandbox: SimpleNamespace, tmp_path: Path):
        outside = tmp_path / "outside_config"
        outside.mkdir()

        with pytest.raises(SandboxViolationError):
            resolve_modsconfig_path(sandbox.rimworld_dir, config_dir=outside)

    def test_auto_resolution_inside_sandbox_still_works(
        self, sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ):
        fake_home = sandbox.root / "home"
        expected = _ludeon_config(fake_home)
        _write_modsconfig(expected, ["auto.mod"])
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        assert resolve_modsconfig_path(sandbox.rimworld_dir) == expected

    def test_auto_resolution_outside_sandbox_fails_closed(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The incident shape: real LocalLow reachable while a sandbox is active."""
        fake_home = tmp_path / "real_looking_home"
        _write_modsconfig(_ludeon_config(fake_home), ["real.mod"])
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        with pytest.raises(SandboxViolationError):
            resolve_modsconfig_path(sandbox.rimworld_dir)


# ── writers stay inside the sandbox ────────────────────────────────


class TestWritersStayInSandbox:
    def test_rimsort_apply_writes_only_config_dir(self, sandbox: SimpleNamespace):
        target = sandbox.config_dir / "ModsConfig.xml"
        _write_modsconfig(target, ["b.mod", "a.mod"])
        _make_mod(sandbox.mods_dir, "A", "a.mod")
        _make_mod(sandbox.mods_dir, "B", "b.mod")

        assert target.is_relative_to(sandbox.root)
        before = _fingerprint(_REAL_MODSCONFIG)

        result = rimsort_apply(cfg=sandbox.cfg)

        assert result["ok"] is True
        written = target.read_text(encoding="utf-8")
        assert "a.mod" in written and "b.mod" in written
        assert list(sandbox.config_dir.glob("ModsConfig.xml.bak_*"))
        assert _fingerprint(_REAL_MODSCONFIG) == before

    def test_profiles_restore_writes_only_config_dir(self, sandbox: SimpleNamespace):
        target = sandbox.config_dir / "ModsConfig.xml"
        _write_modsconfig(target, ["old.mod"])
        save_profile(
            "sandbox-profile",
            "<ModsConfigData><activeMods><li>new.mod</li></activeMods></ModsConfigData>",
        )
        assert (sandbox.profiles_dir / "sandbox-profile.xml").is_relative_to(sandbox.root)
        assert target.is_relative_to(sandbox.root)
        before = _fingerprint(_REAL_MODSCONFIG)

        result = profiles_restore_endpoint("sandbox-profile", cfg=sandbox.cfg)

        assert result["ok"] is True
        assert "new.mod" in target.read_text(encoding="utf-8")
        assert (sandbox.config_dir / "ModsConfig.xml.rwmod.bak").exists()
        assert _fingerprint(_REAL_MODSCONFIG) == before

    def test_undo_restores_only_config_dir(self, sandbox: SimpleNamespace):
        target = sandbox.config_dir / "ModsConfig.xml"
        _write_modsconfig(target, ["current.mod"])
        snapshot = sandbox.undo_dir / "20260101_000000_sort.xml"
        _write_modsconfig(snapshot, ["previous.mod"])

        assert target.is_relative_to(sandbox.root)
        before = _fingerprint(_REAL_MODSCONFIG)

        result = api_undo(cfg=sandbox.cfg)

        assert result["ok"] is True
        assert "previous.mod" in target.read_text(encoding="utf-8")
        assert not snapshot.exists()
        assert _fingerprint(_REAL_MODSCONFIG) == before

    @pytest.mark.parametrize("writer", ["apply", "restore", "undo"])
    def test_writer_refuses_config_dir_outside_sandbox(
        self, sandbox: SimpleNamespace, tmp_path: Path, writer: str
    ):
        outside = tmp_path / "outside_config"
        outside.mkdir()
        outside_target = outside / "ModsConfig.xml"
        _write_modsconfig(outside_target, ["safe.mod"])
        sandbox.cfg.config_dir = outside

        if writer == "restore":
            save_profile("escape-profile", "<ModsConfigData><activeMods/></ModsConfigData>")

        with pytest.raises(SandboxViolationError):
            if writer == "apply":
                rimsort_apply(cfg=sandbox.cfg)
            elif writer == "restore":
                profiles_restore_endpoint("escape-profile", cfg=sandbox.cfg)
            else:
                api_undo(cfg=sandbox.cfg)

        assert outside_target.read_text(encoding="utf-8").count("safe.mod") == 1
        assert not list(outside.glob("ModsConfig.xml.bak_*"))
        assert not (outside / "ModsConfig.xml.rwmod.bak").exists()

    def test_save_profile_refuses_profiles_dir_outside_sandbox(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.profile as profile_mod

        outside = tmp_path / "outside_profiles"
        monkeypatch.setattr(profile_mod, "PROFILES_DIR", outside)

        with pytest.raises(SandboxViolationError):
            save_profile("escape", "<ModsConfigData><activeMods/></ModsConfigData>")
        assert not (outside / "escape.xml").exists()

    def test_restore_profile_guard_raises_instead_of_returning_ok_false(
        self, sandbox: SimpleNamespace, tmp_path: Path
    ):
        """The sandbox guard must not be swallowed by restore_profile's try/except."""
        save_profile("guarded", "<ModsConfigData><activeMods/></ModsConfigData>")
        outside_target = tmp_path / "outside" / "ModsConfig.xml"

        with pytest.raises(SandboxViolationError):
            restore_profile_file("guarded", outside_target)
        assert not outside_target.exists()


# ── CLI ────────────────────────────────────────────────────────────


class TestCliProfileRestore:
    def test_cli_restore_writes_only_config_dir(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        toml = tmp_path / "cli.toml"
        monkeypatch.setattr(Config, "CONFIG_PATH", toml)
        sandbox.cfg.save()

        target = sandbox.config_dir / "ModsConfig.xml"
        _write_modsconfig(target, ["old.mod"])
        save_profile(
            "cli-profile",
            "<ModsConfigData><activeMods><li>cli.mod</li></activeMods></ModsConfigData>",
        )
        assert target.is_relative_to(sandbox.root)
        before = _fingerprint(_REAL_MODSCONFIG)

        result = runner.invoke(app, ["profile-restore", "cli-profile"])

        assert result.exit_code == 0, result.output
        assert "cli.mod" in target.read_text(encoding="utf-8")
        assert _fingerprint(_REAL_MODSCONFIG) == before

    def test_canary_cli_restore_without_sandbox_never_writes_real_path(
        self, sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Canary: no RWMOD_TEST_SANDBOX_HOME, Path.home unpatched (the incident).

        ``config_dir`` alone must be enough to keep the write in tmp.
        """
        monkeypatch.delenv(SANDBOX_ENV_VAR, raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: _REAL_HOME))

        toml = tmp_path / "cli_canary.toml"
        monkeypatch.setattr(Config, "CONFIG_PATH", toml)
        sandbox.cfg.save()

        target = sandbox.config_dir / "ModsConfig.xml"
        _write_modsconfig(target, ["old.mod"])
        save_profile(
            "canary-profile",
            "<ModsConfigData><activeMods><li>canary.mod</li></activeMods></ModsConfigData>",
        )
        before = _fingerprint(_REAL_MODSCONFIG)

        result = runner.invoke(app, ["profile-restore", "canary-profile"])

        assert result.exit_code == 0, result.output
        assert "canary.mod" in target.read_text(encoding="utf-8")
        assert _fingerprint(_REAL_MODSCONFIG) == before


# ── canary without the sandbox marker ──────────────────────────────


def test_canary_apply_without_sandbox_never_writes_real_path(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    """Without the sandbox marker and with the real Path.home, an explicit
    config_dir still captures the write — this is the incident's exact setup."""
    monkeypatch.delenv(SANDBOX_ENV_VAR, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: _REAL_HOME))

    target = sandbox.config_dir / "ModsConfig.xml"
    _write_modsconfig(target, ["a.mod"])
    _make_mod(sandbox.mods_dir, "A", "a.mod")
    before = _fingerprint(_REAL_MODSCONFIG)

    result = rimsort_apply(cfg=sandbox.cfg)

    assert result["ok"] is True
    assert target.exists()
    assert _fingerprint(_REAL_MODSCONFIG) == before
