"""RimWorld per-mod settings files (Config/Mod_*.xml).

Covers listing/association, raw-XML read, validated writes (2 MB cap,
well-formedness, filename whitelist, backup-before-write, BOM/CRLF
preservation) and backup/restore.

Every write test runs inside a tmp sandbox: ``RWMOD_TEST_SANDBOX_HOME`` is set,
``Path.home`` is redirected to a tmp dir (the backup root is resolved at call
time), and the target is asserted to live under the sandbox *before* writing.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rwmod.errors import ModNotFoundError, ValidationError
from rwmod.mod_settings import (
    backup_all,
    backups_dir,
    is_game_running,
    list_backups,
    list_mod_settings,
    read_mod_setting,
    resolve_config_dir,
    restore_backup,
    write_mod_setting,
)
from rwmod.profile import SANDBOX_ENV_VAR, SandboxViolationError

_MAX = 2 * 1024 * 1024
_BOM = b"\xef\xbb\xbf"
_XML = "<ModSettings><value>1</value></ModSettings>"


# ── helpers ────────────────────────────────────────────────────────


def _ludeon_config_dir(home: Path, folder: str = "RimWorld by Ludeon Studios") -> Path:
    if platform.system() == "Windows":
        base = home / "AppData" / "LocalLow" / "Ludeon Studios"
    elif platform.system() == "Darwin":
        base = home / "Library" / "Application Support" / "Ludeon Studios"
    else:
        base = home / ".config" / "unity3d" / "Ludeon Studios"
    return base / folder / "Config"


def _make_mod(mods_dir: Path, folder: str, package_id: str, workshop_id: str = "") -> None:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{folder}</name><packageId>{package_id}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    if workshop_id:
        (about / "PublishedFileId.txt").write_text(workshop_id, encoding="utf-8")


def _fingerprint(path: Path) -> tuple[int, str]:
    st = path.stat()
    return st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """tmp Config/Mods dirs plus a tmp home; every write target is under root."""
    root = tmp_path / "sandbox"
    config_dir = root / "Config"
    mods_dir = root / "Mods"
    home = root / "home"
    for d in (config_dir, mods_dir, home):
        d.mkdir(parents=True)

    monkeypatch.setenv(SANDBOX_ENV_VAR, str(root))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("rwmod.mod_settings.is_game_running", lambda: False)

    return SimpleNamespace(
        root=root,
        config_dir=config_dir,
        mods_dir=mods_dir,
        home=home,
        backup_root=home / ".rwmod" / "mod_settings_backups",
    )


# ── config dir resolution ─────────────────────────────────────────


class TestResolveConfigDir:
    def test_explicit_dir_wins(self, sandbox: SimpleNamespace):
        assert resolve_config_dir(sandbox.config_dir) == sandbox.config_dir

    def test_missing_explicit_dir_is_none(self, sandbox: SimpleNamespace):
        assert resolve_config_dir(sandbox.root / "nope") is None

    def test_auto_probes_local_low(self, sandbox: SimpleNamespace):
        expected = _ludeon_config_dir(sandbox.home)
        expected.mkdir(parents=True)

        assert resolve_config_dir(None) == expected

    def test_auto_probe_without_game_is_none(self, sandbox: SimpleNamespace):
        assert resolve_config_dir(None) is None

    def test_explicit_dir_outside_sandbox_fails_closed(
        self, sandbox: SimpleNamespace, tmp_path: Path
    ):
        outside = tmp_path / "outside" / "Config"
        outside.mkdir(parents=True)

        with pytest.raises(SandboxViolationError):
            resolve_config_dir(outside)


# ── listing / association ─────────────────────────────────────────


class TestListModSettings:
    def test_lists_only_mod_settings_and_associates_both_naming_schemes(
        self, sandbox: SimpleNamespace
    ):
        _make_mod(sandbox.mods_dir, "Rimatomics", "Dubwise.Rimatomics", "1127530465")
        _make_mod(sandbox.mods_dir, "Mixed", "Author.MixedCase", "999")
        (sandbox.config_dir / "Mod_1127530465_RimatomicsMod.xml").write_text(_XML)
        (sandbox.config_dir / "Mod_Author.MixedCase_MixedMod.xml").write_text(_XML)
        (sandbox.config_dir / "Mod_555_Unknown.xml").write_text(_XML)
        (sandbox.config_dir / "KeyPrefs.xml").write_text("<a/>")
        (sandbox.config_dir / "ModFeatures").mkdir()

        data = list_mod_settings(sandbox.config_dir, sandbox.mods_dir)
        assert data["config_dir"] == str(sandbox.config_dir)
        files = {f["filename"]: f for f in data["files"]}

        assert set(files) == {
            "Mod_1127530465_RimatomicsMod.xml",
            "Mod_Author.MixedCase_MixedMod.xml",
            "Mod_555_Unknown.xml",
        }
        numeric = files["Mod_1127530465_RimatomicsMod.xml"]
        assert numeric["associated"] is True
        assert numeric["workshop_id"] == "1127530465"
        assert numeric["package_id"] == "Dubwise.Rimatomics"
        assert numeric["name"] == "Rimatomics"

        # About.xml casing differs from the settings filename.
        mixed = files["Mod_Author.MixedCase_MixedMod.xml"]
        assert mixed["associated"] is True
        assert mixed["package_id"] == "Author.MixedCase"

        unknown = files["Mod_555_Unknown.xml"]
        assert unknown["associated"] is False
        assert unknown["workshop_id"] == "555"
        assert unknown["package_id"] == ""

    def test_no_config_dir_returns_empty_list(self, sandbox: SimpleNamespace):
        data = list_mod_settings(None, sandbox.mods_dir)
        assert data["config_dir"] is None
        assert data["files"] == []


# ── read ──────────────────────────────────────────────────────────


class TestReadModSetting:
    def test_returns_raw_xml_and_summary(self, sandbox: SimpleNamespace):
        target = sandbox.config_dir / "Mod_1_Thing.xml"
        target.write_text(_XML, encoding="utf-8")

        data = read_mod_setting("Mod_1_Thing.xml", sandbox.config_dir)

        assert data["xml"] == _XML
        assert data["well_formed"] is True
        assert data["root_tag"] == "ModSettings"
        assert data["size"] == target.stat().st_size
        assert data["mtime"]

    def test_malformed_file_is_shown_not_rejected(self, sandbox: SimpleNamespace):
        (sandbox.config_dir / "Mod_1_Broken.xml").write_text("<broken>", encoding="utf-8")

        data = read_mod_setting("Mod_1_Broken.xml", sandbox.config_dir)

        assert data["well_formed"] is False
        assert data["root_tag"] == ""
        assert data["xml"].startswith("<broken>")

    def test_missing_file_is_404(self, sandbox: SimpleNamespace):
        with pytest.raises(ModNotFoundError):
            read_mod_setting("Mod_1_Nope.xml", sandbox.config_dir)

    @pytest.mark.parametrize(
        "name",
        ["../Mod_x.xml", "Mod_x.xml/../evil.xml", "KeyPrefs.xml", "Mod_x.txt", "mod_x.xml"],
    )
    def test_whitelist_rejects_other_names(self, sandbox: SimpleNamespace, name: str):
        with pytest.raises(ValidationError):
            read_mod_setting(name, sandbox.config_dir)


# ── write ─────────────────────────────────────────────────────────


class TestWriteModSetting:
    def _target(self, sandbox: SimpleNamespace) -> Path:
        target = sandbox.config_dir / "Mod_1_Thing.xml"
        target.write_bytes(_BOM + _XML.replace(">", ">\r\n").encode("utf-8"))
        assert target.is_relative_to(sandbox.root)
        return target

    def test_requires_ack_game_closed(self, sandbox: SimpleNamespace):
        target = self._target(sandbox)
        before = _fingerprint(target)

        with pytest.raises(ValidationError):
            write_mod_setting(
                "Mod_1_Thing.xml", _XML, config_dir=sandbox.config_dir, ack_game_closed=False
            )

        assert _fingerprint(target) == before

    def test_rejects_malformed_xml(self, sandbox: SimpleNamespace):
        target = self._target(sandbox)
        before = _fingerprint(target)

        with pytest.raises(ValidationError):
            write_mod_setting(
                "Mod_1_Thing.xml", "<broken>", config_dir=sandbox.config_dir, ack_game_closed=True
            )

        assert _fingerprint(target) == before

    def test_rejects_oversized_content(self, sandbox: SimpleNamespace):
        target = self._target(sandbox)
        before = _fingerprint(target)

        with pytest.raises(ValidationError):
            write_mod_setting(
                "Mod_1_Thing.xml",
                "<a>" + ("x" * (_MAX + 1)) + "</a>",
                config_dir=sandbox.config_dir,
                ack_game_closed=True,
            )

        assert _fingerprint(target) == before

    def test_missing_file_is_404(self, sandbox: SimpleNamespace):
        with pytest.raises(ModNotFoundError):
            write_mod_setting(
                "Mod_1_Nope.xml", _XML, config_dir=sandbox.config_dir, ack_game_closed=True
            )

    @pytest.mark.parametrize("name", ["../Mod_1_Thing.xml", "KeyPrefs.xml", "Mod_1_Thing.txt"])
    def test_whitelist_rejects_other_names(self, sandbox: SimpleNamespace, name: str):
        with pytest.raises(ValidationError):
            write_mod_setting(name, _XML, config_dir=sandbox.config_dir, ack_game_closed=True)

    def test_writes_atomically_and_backs_up_first(self, sandbox: SimpleNamespace):
        target = self._target(sandbox)
        old_bytes = target.read_bytes()

        result = write_mod_setting(
            "Mod_1_Thing.xml",
            "<ModSettings><value>2</value></ModSettings>",
            config_dir=sandbox.config_dir,
            ack_game_closed=True,
        )

        assert result["ok"] is True
        assert result["game_running"] is False
        # Backups are partitioned by instance (<root>/<instance_id>/), so the
        # expected directory is the one the module itself reports.
        backup = backups_dir() / result["backup"]
        assert backup.is_relative_to(sandbox.root)
        assert backup.read_bytes() == old_bytes
        assert b"<value>2</value>" in target.read_bytes()
        assert not list(sandbox.config_dir.glob("*.rwmod.tmp"))

    def test_preserves_bom_and_crlf_style(self, sandbox: SimpleNamespace):
        target = self._target(sandbox)

        write_mod_setting(
            "Mod_1_Thing.xml",
            "<ModSettings>\n<value>2</value>\n</ModSettings>",  # LF, no BOM
            config_dir=sandbox.config_dir,
            ack_game_closed=True,
        )

        raw = target.read_bytes()
        assert raw.startswith(_BOM)
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    def test_lf_file_stays_lf(self, sandbox: SimpleNamespace):
        target = sandbox.config_dir / "Mod_2_Lf.xml"
        target.write_bytes(_XML.encode("utf-8"))

        write_mod_setting(
            "Mod_2_Lf.xml",
            "<ModSettings>\r\n<value>2</value>\r\n</ModSettings>",
            config_dir=sandbox.config_dir,
            ack_game_closed=True,
        )

        raw = target.read_bytes()
        assert b"\r\n" not in raw
        assert not raw.startswith(_BOM)

    def test_game_running_only_warns(self, sandbox: SimpleNamespace, monkeypatch):
        target = self._target(sandbox)
        monkeypatch.setattr("rwmod.mod_settings.is_game_running", lambda: True)

        result = write_mod_setting(
            "Mod_1_Thing.xml", _XML, config_dir=sandbox.config_dir, ack_game_closed=True
        )

        assert result["ok"] is True
        assert result["game_running"] is True
        assert result["warning"]
        assert b"<ModSettings>" in target.read_bytes()

    def test_write_outside_sandbox_fails_closed(self, sandbox: SimpleNamespace, tmp_path: Path):
        outside = tmp_path / "outside" / "Config"
        outside.mkdir(parents=True)
        target = outside / "Mod_1_Thing.xml"
        target.write_text(_XML, encoding="utf-8")
        before = _fingerprint(target)

        with pytest.raises(SandboxViolationError):
            write_mod_setting("Mod_1_Thing.xml", _XML, config_dir=outside, ack_game_closed=True)

        assert _fingerprint(target) == before


# ── backup / restore ──────────────────────────────────────────────


class TestBackupAndRestore:
    def _seed(self, sandbox: SimpleNamespace) -> tuple[Path, str]:
        target = sandbox.config_dir / "Mod_1_Thing.xml"
        target.write_text(_XML, encoding="utf-8")
        result = write_mod_setting(
            "Mod_1_Thing.xml",
            "<ModSettings><value>2</value></ModSettings>",
            config_dir=sandbox.config_dir,
            ack_game_closed=True,
        )
        return target, result["backup"]

    def test_backup_all_copies_every_mod_setting(self, sandbox: SimpleNamespace):
        (sandbox.config_dir / "Mod_1_A.xml").write_text(_XML, encoding="utf-8")
        (sandbox.config_dir / "Mod_2_B.xml").write_text(_XML, encoding="utf-8")
        (sandbox.config_dir / "KeyPrefs.xml").write_text("<a/>", encoding="utf-8")

        result = backup_all(sandbox.config_dir)

        assert result["ok"] is True
        assert result["count"] == 2
        names = {row["filename"] for row in result["backups"]}
        assert names == {"Mod_1_A.xml", "Mod_2_B.xml"}
        assert backups_dir().is_relative_to(sandbox.root)
        assert len(list(backups_dir().glob("*.xml"))) == 2

    def test_backup_all_without_config_dir_is_empty(self, sandbox: SimpleNamespace):
        result = backup_all(None)
        assert result["count"] == 0
        assert result["backups"] == []

    def test_restore_latest_backup(self, sandbox: SimpleNamespace):
        target, backup_name = self._seed(sandbox)
        assert b"<value>2</value>" in target.read_bytes()

        result = restore_backup(
            "Mod_1_Thing.xml", config_dir=sandbox.config_dir, ack_game_closed=True
        )

        assert result["ok"] is True
        assert result["restored_from"] == backup_name
        assert result["backup"]  # the pre-restore state is backed up too
        assert b"<value>1</value>" in target.read_bytes()

    def test_restore_latest_handles_glob_metacharacters_in_filename(self, sandbox: SimpleNamespace):
        """A real filename may contain '[' / ']' — it must not be globbed."""
        target = sandbox.config_dir / "Mod_[Foo]_Bar.xml"
        target.write_text("<v1/>", encoding="utf-8")
        write_mod_setting(
            "Mod_[Foo]_Bar.xml",
            "<v2/>",
            config_dir=sandbox.config_dir,
            ack_game_closed=True,
        )

        result = restore_backup(
            "Mod_[Foo]_Bar.xml", config_dir=sandbox.config_dir, ack_game_closed=True
        )

        assert result["restored_from"]
        assert target.read_text(encoding="utf-8") == "<v1/>"

    def test_restore_specific_backup(self, sandbox: SimpleNamespace):
        target, backup_name = self._seed(sandbox)

        result = restore_backup(
            "Mod_1_Thing.xml",
            backup_name,
            config_dir=sandbox.config_dir,
            ack_game_closed=True,
        )

        assert result["restored_from"] == backup_name
        assert b"<value>1</value>" in target.read_bytes()

    def test_restore_requires_ack(self, sandbox: SimpleNamespace):
        target, _ = self._seed(sandbox)
        before = _fingerprint(target)

        with pytest.raises(ValidationError):
            restore_backup("Mod_1_Thing.xml", config_dir=sandbox.config_dir, ack_game_closed=False)

        assert _fingerprint(target) == before

    def test_restore_without_backups_is_404(self, sandbox: SimpleNamespace):
        (sandbox.config_dir / "Mod_9_Never.xml").write_text(_XML, encoding="utf-8")

        with pytest.raises(ModNotFoundError):
            restore_backup("Mod_9_Never.xml", config_dir=sandbox.config_dir, ack_game_closed=True)

    @pytest.mark.parametrize("bad", ["../evil.xml", "sub/Mod_1_Thing.xml", "x.txt"])
    def test_restore_rejects_traversal_backup_names(self, sandbox: SimpleNamespace, bad: str):
        self._seed(sandbox)

        with pytest.raises(ValidationError):
            restore_backup(
                "Mod_1_Thing.xml", bad, config_dir=sandbox.config_dir, ack_game_closed=True
            )

    def test_restore_rejects_another_files_backup(self, sandbox: SimpleNamespace):
        _, backup_name = self._seed(sandbox)
        (sandbox.config_dir / "Mod_3_Other.xml").write_text(_XML, encoding="utf-8")

        with pytest.raises(ValidationError):
            restore_backup(
                "Mod_3_Other.xml",
                backup_name,
                config_dir=sandbox.config_dir,
                ack_game_closed=True,
            )

    def test_list_backups_reports_source_and_is_newest_first(self, sandbox: SimpleNamespace):
        self._seed(sandbox)
        (sandbox.config_dir / "Mod_1_Thing.xml").write_text(
            "<ModSettings><value>3</value></ModSettings>", encoding="utf-8"
        )
        write_mod_setting(
            "Mod_1_Thing.xml",
            "<ModSettings><value>4</value></ModSettings>",
            config_dir=sandbox.config_dir,
            ack_game_closed=True,
        )

        data = list_backups()

        assert data["backup_dir"] == str(backups_dir())
        assert len(data["backups"]) == 2
        assert all(b["source"] == "Mod_1_Thing.xml" for b in data["backups"])
        assert data["backups"][0]["mtime"] >= data["backups"][1]["mtime"]


# ── game process probe ────────────────────────────────────────────


class TestGameRunningProbe:
    def test_detects_no_game_when_tasklist_finds_nothing(self, monkeypatch):
        def fake_run(*_args, **_kwargs):
            return SimpleNamespace(stdout="INFO: No tasks are running...", returncode=0)

        # Pin the probe to the Windows/tasklist branch so a POSIX CI runner
        # does not take the pgrep path (where returncode=0 means "running").
        monkeypatch.setattr("rwmod.mod_settings.platform.system", lambda: "Windows")
        monkeypatch.setattr("rwmod.mod_settings.subprocess.run", fake_run)
        assert is_game_running() is False

    def test_detects_no_game_on_posix_when_pgrep_finds_nothing(self, monkeypatch):
        def fake_run(*_args, **_kwargs):
            return SimpleNamespace(stdout="", returncode=1)

        monkeypatch.setattr("rwmod.mod_settings.platform.system", lambda: "Linux")
        monkeypatch.setattr("rwmod.mod_settings.subprocess.run", fake_run)
        assert is_game_running() is False

    def test_detects_game_on_posix_when_pgrep_matches(self, monkeypatch):
        def fake_run(*_args, **_kwargs):
            return SimpleNamespace(stdout="4242 RimWorld", returncode=0)

        monkeypatch.setattr("rwmod.mod_settings.platform.system", lambda: "Linux")
        monkeypatch.setattr("rwmod.mod_settings.subprocess.run", fake_run)
        assert is_game_running() is True

    def test_probe_never_raises(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("tasklist", 3)

        monkeypatch.setattr("rwmod.mod_settings.subprocess.run", boom)
        assert is_game_running() is False
