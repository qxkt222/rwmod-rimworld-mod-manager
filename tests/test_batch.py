"""Batch download tests — one SteamCMD process for many mods.

Covers download_batch() orchestration and SteamCMD.workshop_download_many()
input handling without touching the network (SteamCMD is mocked / never
actually spawned for valid ids).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.config import Config
from rwmod.downloader import (
    _collect_deps,
    _find_existing_many,
    _install_content,
    download_batch,
)
from rwmod.steamcmd import SteamCMD


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path: Path):
    """Point every DB touch at a temp file (and drop the thread-local
    connection cache first) so these tests never read/write the real
    ~/.rwmod.db — the connection cache would otherwise leak across tests
    because this module does not use the conftest ``client`` fixture."""
    from rwmod import database as db

    with patch("rwmod.database.DB_PATH", tmp_path / "test.db"):
        db.close_db()
        yield
        db.close_db()


def _cfg(tmp_path: Path) -> Config:
    exe = tmp_path / "steamcmd" / "steamcmd.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.touch()
    cfg = Config(steamcmd_path=exe, mods_dir=tmp_path / "Mods")
    cfg.mods_dir.mkdir()
    return cfg


def _installed_mod(mods_dir: Path, folder: str, wid: str) -> None:
    d = mods_dir / folder
    (d / "About").mkdir(parents=True)
    (d / "About" / "PublishedFileId.txt").write_text(wid, encoding="utf-8")


def _workshop_content(tmp_path: Path, cfg: Config, wid: str, pkg: str = "author.mod") -> Path:
    """Create SteamCMD's downloaded content dir for a fake mod."""
    content = (
        tmp_path / "steamcmd" / "steamapps" / "workshop" / "content" / SteamCMD.STEAM_APP_ID / wid
    )
    (content / "About").mkdir(parents=True)
    (content / "About" / "About.xml").write_text(
        f"<ModMetaData><name>Test {wid}</name><packageId>{pkg}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    (content / "About" / "PublishedFileId.txt").write_text(wid, encoding="utf-8")
    return content


class TestDownloadManyInvalidIds:
    def test_invalid_ids_rejected_without_spawning(self, tmp_path: Path):
        exe = tmp_path / "steamcmd.exe"
        exe.touch()
        sc = SteamCMD(exe)
        results = sc.workshop_download_many(["abc", "12x", " 123 "])
        assert all(not r.success for r in results.values())
        assert all("非法" in r.error_detail for r in results.values())

    def test_empty_batch(self, tmp_path: Path):
        exe = tmp_path / "steamcmd.exe"
        exe.touch()
        assert SteamCMD(exe).workshop_download_many([]) == {}


class TestDownloadBatch:
    def test_all_installed_skips_steamcmd(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _installed_mod(cfg.mods_dir, "ModA", "111")
        called: list[list[str]] = []

        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None: called.append(ids) or {},
        )
        result = download_batch(cfg, ["111"])
        assert result == {"111": True}
        assert called == []  # no SteamCMD invocation at all

    def test_successful_batch_installs_all(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        for wid, pkg in (("111", "author.one"), ("222", "author.two"), ("333", "author.three")):
            _workshop_content(tmp_path, cfg, wid, pkg=pkg)

        def fake_download_many(self, ids: list[str], progress_cb=None):
            from rwmod.steamcmd import DownloadResult, ErrorKind

            return {mid: DownloadResult(True, mid, ErrorKind.OK) for mid in ids}

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_download_many)
        result = download_batch(cfg, ["111", "222", "333"])
        assert result == {"111": True, "222": True, "333": True}
        # Each mod folder installed under its packageId
        for pkg in ("author.one", "author.two", "author.three"):
            assert (cfg.mods_dir / pkg).is_dir()
            assert (cfg.mods_dir / pkg / "About" / "About.xml").is_file()

    def test_missing_result_entry_is_failure(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _workshop_content(tmp_path, cfg, "111")

        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None: {},  # process died without writing any result
        )
        # Recoverable-failure path would retry via download_one → patch it to
        # avoid a real SteamCMD spawn.
        monkeypatch.setattr("rwmod.downloader.download_one", lambda c, mid, force=False: False)
        result = download_batch(cfg, ["111"])
        assert result == {"111": False}

    def test_collection_children_go_to_extra_out(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)

        def fake_is_collection(mid: str) -> bool:
            return mid == "999999999"

        def fake_children(mid: str) -> list[str]:
            return ["100", "200"]

        monkeypatch.setattr("rwmod.workshop.is_collection", fake_is_collection)
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", fake_children)
        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None: {},
        )
        monkeypatch.setattr("rwmod.downloader.download_one", lambda c, mid, force=False: False)

        extra: list[str] = []
        result = download_batch(cfg, ["999999999", "333"], extra_out=extra)
        assert result["999999999"] is True  # collection itself is not a mod
        assert set(extra) == {"100", "200"}
        # "333" was downloaded by the (empty) batch → failure, no SteamCMD spawn
        assert result["333"] is False

    def test_non_retryable_failure_skips_retry(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        from rwmod.steamcmd import DownloadResult, ErrorKind

        retried: list[str] = []

        def fake_download_many(self, ids: list[str], progress_cb=None):
            return {
                "111": DownloadResult(False, "111", ErrorKind.FILE_NOT_FOUND, "gone"),
            }

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_download_many)
        monkeypatch.setattr(
            "rwmod.downloader.download_one",
            lambda c, mid, force=False: retried.append(mid) or True,
        )
        result = download_batch(cfg, ["111"])
        assert result == {"111": False}
        assert retried == []  # permanent error → no retry, no Skymods

    def test_recoverable_failure_retried_via_download_one(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        from rwmod.steamcmd import DownloadResult, ErrorKind

        retried: list[str] = []

        def fake_download_many(self, ids: list[str], progress_cb=None):
            return {"111": DownloadResult(False, "111", ErrorKind.UNKNOWN, "blip")}

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_download_many)
        monkeypatch.setattr(
            "rwmod.downloader.download_one",
            lambda c, mid, force=False: retried.append(mid) or True,
        )
        result = download_batch(cfg, ["111"])
        assert result == {"111": True}
        assert retried == ["111"]


class TestFindExistingMany:
    def test_hits_and_misses(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        _installed_mod(cfg.mods_dir, "ModA", "111")
        _installed_mod(cfg.mods_dir, "ModB", "222")
        result = _find_existing_many(cfg.mods_dir, ["111", "222", "333"])
        assert result["111"] is not None and result["111"].name == "ModA"
        assert result["222"] is not None and result["222"].name == "ModB"
        assert result["333"] is None

    def test_single_scan_covers_all_missing(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _installed_mod(cfg.mods_dir, "ModC", "777")

        # Force the DB path to miss so the fallback scan runs once for the
        # whole batch (never one full scan per id).
        monkeypatch.setattr("rwmod.downloader.find_local_mods_by_workshop_ids", lambda ids: {})
        result = _find_existing_many(cfg.mods_dir, ["777", "888", "999"])
        assert result["777"] is not None and result["777"].name == "ModC"
        assert result["888"] is None and result["999"] is None


class TestCollectDepsLevelBatch:
    def test_level_queried_in_one_call(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _installed_mod(cfg.mods_dir, "Installed", "500")
        calls: list[list[str]] = []

        def fake_deps(mod_ids: list[str]) -> dict[str, list[str]]:
            calls.append(mod_ids)
            out: dict[str, list[str]] = {}
            for mid in mod_ids:
                if mid == "100":
                    out["100"] = ["200", "500", "300"]
                elif mid == "200":
                    out["200"] = ["300"]
                elif mid == "300":
                    out["300"] = []
            return out

        monkeypatch.setattr("rwmod.workshop.fetch_item_dependencies", fake_deps)
        deferred: list[str] = []
        _collect_deps(cfg, "100", deferred)
        # Level 1 = ["100"] (1 call), level 2 = ["200", "300"] (1 call)
        assert calls == [["100"], ["200", "300"]]
        # 500 already installed → skipped; 200, 300 collected once each
        assert sorted(deferred) == ["200", "300"]


class TestInstallContent:
    def test_copies_to_mods_dir(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        _workshop_content(tmp_path, cfg, "111")
        assert _install_content(cfg, "111") is True
        assert (cfg.mods_dir / "author.mod" / "About" / "About.xml").is_file()

    def test_missing_content_returns_false(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        assert _install_content(cfg, "999") is False

    def test_no_about_xml_returns_false(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        content = (
            tmp_path
            / "steamcmd"
            / "steamapps"
            / "workshop"
            / "content"
            / SteamCMD.STEAM_APP_ID
            / "555"
        )
        (content / "About").mkdir(parents=True)  # no About.xml inside
        assert _install_content(cfg, "555") is False
