"""Smoke tests for the `rwmod` CLI.

`rwmod = "rwmod.cli:app"` is a shipped entry point (pyproject.toml) and had 0%
coverage, so a break in it shipped silently. These drive it through typer's
CliRunner against an isolated config — no SteamCMD, no network, no user files.
Commands that genuinely need the network or a real SteamCMD (`web`, `setup`,
`download` against a live id) are out of scope by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rwmod.cli import app

runner = CliRunner()


def _output(result) -> str:
    """stdout + stderr.

    ``typer.echo(..., err=True)`` writes to stderr, and whether CliRunner folds
    that into ``.output`` depends on the click version — read both.
    """
    return result.output + getattr(result, "stderr", "")


@pytest.fixture
def cli_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Config at a throwaway file so CLI tests never touch ~/.rwmod.toml.

    Path.home is redirected too: resolve_modsconfig_path() consults the user's
    LocalLow profile before cfg.rimworld_dir, so without it `check-order` would
    analyze the developer's real RimWorld install instead of the temp one.
    """
    import rwmod.config as cfg_mod

    monkeypatch.setattr(cfg_mod.Config, "CONFIG_PATH", tmp_path / ".rwmod_cli.toml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    mods = tmp_path / "Mods"
    mods.mkdir()
    (tmp_path / "RimWorld").mkdir()
    # `rwmod download` calls cfg.validate(), which requires steamcmd to exist.
    # Creating it is not optional bookkeeping: Config._load_no_cache falls back
    # to the *bundled* steamcmd when the configured path is missing, so without
    # this file the test passes only on a machine that happens to have a
    # steamcmd/ checkout and fails on a clean one (CI). conftest's client
    # fixture creates the same file for the same reason.
    (tmp_path / "steamcmd").mkdir()
    (tmp_path / "steamcmd" / "steamcmd.exe").touch()
    cfg_mod.Config(
        steamcmd_path=tmp_path / "steamcmd" / "steamcmd.exe",
        mods_dir=mods,
        rimworld_dir=tmp_path / "RimWorld",
    ).save()
    return tmp_path


def _write_mod(root: Path, folder: str, name: str, pkg: str, wid: str | None) -> None:
    about = root / "Mods" / folder / "About"
    about.mkdir(parents=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{name}</name><packageId>{pkg}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    if wid is not None:
        (about / "PublishedFileId.txt").write_text(wid, encoding="utf-8")


class TestShowConfig:
    def test_prints_the_paths(self, cli_config: Path):
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0
        assert "Mods 目录" in _output(result)


class TestList:
    def test_empty_mods_dir(self, cli_config: Path):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "已安装 Mod (0 个)" in _output(result)

    def test_lists_name_package_id_and_workshop_id(self, cli_config: Path):
        _write_mod(cli_config, "SomeMod", "Some Mod", "some.mod", "2009463077")
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        out = _output(result)
        assert "Some Mod" in out
        assert "some.mod" in out
        assert "2009463077" in out

    def test_non_numeric_published_file_id_is_not_shown(self, cli_config: Path):
        """The numeric-ID guard from metadata.read_mod_metadata reaches the CLI."""
        _write_mod(cli_config, "Evil", "Evil", "evil.mod", "../evil")
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "../evil" not in _output(result)


class TestCheckOrder:
    def test_missing_modsconfig_reports_rather_than_crashes(self, cli_config: Path):
        result = runner.invoke(app, ["check-order"])
        assert result.exit_code == 0
        assert "ModsConfig" in _output(result)


class TestCompat:
    def test_no_rimworld_version_is_reported(self, cli_config: Path):
        result = runner.invoke(app, ["compat"])
        assert result.exit_code == 0
        assert "未能检测到 RimWorld 版本" in _output(result)


class TestDownload:
    def test_unparseable_id_exits_nonzero(self, cli_config: Path):
        result = runner.invoke(app, ["download", "not-a-number"])
        assert result.exit_code == 1
        assert "跳过无效输入" in _output(result)
