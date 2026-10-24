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
        # P0-1 made collection probing fail-closed, so a unit test must not
        # depend on the real Steam API being reachable.
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
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

    def test_collection_children_skip_installed(self, tmp_path: Path, monkeypatch):
        """Already-installed children must not be handed back to the caller.

        Re-queueing them made every collection run report hundreds of
        "downloads" for mods that were already on disk.
        """
        cfg = _cfg(tmp_path)
        _installed_mod(cfg.mods_dir, "AlreadyThere", "100")

        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: mid == "999999999")
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: ["100", "200"])

        extra: list[str] = []
        result = download_batch(cfg, ["999999999"], extra_out=extra)
        assert result["999999999"] is True
        assert extra == ["200"]

    def test_collection_children_kept_when_force(self, tmp_path: Path, monkeypatch):
        """An update run (force) must still revisit installed children."""
        cfg = _cfg(tmp_path)
        _installed_mod(cfg.mods_dir, "AlreadyThere", "100")

        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: mid == "999999999")
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: ["100", "200"])

        extra: list[str] = []
        download_batch(cfg, ["999999999"], force=True, extra_out=extra)
        assert extra == ["100", "200"]

    def test_collection_all_children_installed_never_reaches_steamcmd(
        self, tmp_path: Path, monkeypatch
    ):
        """K-2: a collection whose children are all installed must never be
        handed to SteamCMD as if it were a single mod.

        The old code only filtered the batch ``todo`` inside ``if children:``,
        so an all-installed collection produced an empty ``children`` list —
        leaving the collection id in ``todo`` (SteamCMD then "downloads" the
        collection, install fails with ``no About.xml`` and Skymods returns 403).
        """
        cfg = _cfg(tmp_path)
        _installed_mod(cfg.mods_dir, "AlreadyOne", "100")
        _installed_mod(cfg.mods_dir, "AlreadyTwo", "200")

        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: mid == "999999999")
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: ["100", "200"])
        steamcmd_calls: list[list[str]] = []

        def fake_many(self, ids, progress_cb=None):
            steamcmd_calls.append(list(ids))
            return {}

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_many)
        monkeypatch.setattr("rwmod.downloader.download_one", lambda c, mid, force=False: True)

        extra: list[str] = []
        result = download_batch(cfg, ["999999999"], extra_out=extra)

        assert result["999999999"] is True  # nothing to do, but not a failure
        assert steamcmd_calls == []  # the collection id never reaches SteamCMD
        assert extra == []  # every child is installed, so nothing is re-queued

    def test_collection_probe_error_fails_without_steamcmd(self, tmp_path: Path, monkeypatch):
        """K-2: a failed collection probe must fail the id explicitly.

        Falling through to SteamCMD is exactly how a collection used to be
        downloaded as a single mod. The id must not be handed to SteamCMD.
        """
        cfg = _cfg(tmp_path)

        def boom(mod_id: str) -> bool:
            raise RuntimeError("api down")

        monkeypatch.setattr("rwmod.workshop.is_collection", boom)
        steamcmd_calls: list[list[str]] = []

        def fake_many(self, ids, progress_cb=None):
            steamcmd_calls.append(list(ids))
            return {m: True for m in ids}

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_many)

        assert download_batch(cfg, ["999999999"]) == {"999999999": False}
        assert steamcmd_calls == []

    def test_empty_collection_fails_without_steamcmd(self, tmp_path: Path, monkeypatch):
        """K-2: an empty/unfetchable collection is an explicit failure."""
        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: True)
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: [])
        steamcmd_calls: list[list[str]] = []

        def fake_many(self, ids, progress_cb=None):
            steamcmd_calls.append(list(ids))
            return {m: True for m in ids}

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_many)

        assert download_batch(cfg, ["999999999"]) == {"999999999": False}
        assert steamcmd_calls == []

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
            lambda c, mid, force=False, outcome_out=None: retried.append(mid) or True,
        )
        result = download_batch(cfg, ["111"])
        assert result == {"111": True}
        assert retried == ["111"]

    def test_batch_result_carries_structured_outcome(self, tmp_path: Path, monkeypatch):
        """P0-2: the historical dict return now carries per-id reasons."""
        from rwmod.steamcmd import DownloadResult, ErrorKind

        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None: {
                m: DownloadResult(False, m, ErrorKind.FILE_NOT_FOUND, "SteamCMD: File Not Found")
                for m in ids
            },
        )

        out_param: dict = {}
        result = download_batch(cfg, ["111"], outcomes=out_param)

        assert result == {"111": False}  # bool contract unchanged
        assert result.outcomes["111"].error_kind == ErrorKind.FILE_NOT_FOUND
        assert result.outcomes["111"].error_detail == "SteamCMD: File Not Found"
        assert out_param["111"] == result.outcomes["111"]  # optional out-param filled

    def test_install_failure_reports_its_reason(self, tmp_path: Path, monkeypatch):
        """P0-2: an install failure must say why, not "unknown"."""
        from rwmod.downloader import KIND_INSTALL_FAILED
        from rwmod.steamcmd import DownloadResult

        cfg = _cfg(tmp_path)
        cfg.enable_skymods = True  # the detail only mentions Skymods when enabled
        _workshop_content(tmp_path, cfg, "111", "author.mod")
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None: {m: DownloadResult(True, m) for m in ids},
        )

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("rwmod.downloader.shutil.copytree", boom)
        monkeypatch.setattr("rwmod.downloader._skymods_fallback", lambda config, mid: False)

        result = download_batch(cfg, ["111"])
        outcome = result.outcomes["111"]
        assert outcome.ok is False
        assert outcome.error_kind == KIND_INSTALL_FAILED
        assert "复制下载内容失败" in outcome.error_detail
        assert "Skymods 备用源也失败" in outcome.error_detail


class TestRetryBackoff:
    """P2-1: 5s → 15s → 45s with ±20% jitter (3 retries after the first try)."""

    def test_delay_sequence(self, monkeypatch):
        from rwmod.downloader import _retry_delay

        monkeypatch.setattr("rwmod.downloader.random.uniform", lambda a, b: 1.0)
        assert [_retry_delay(1), _retry_delay(2), _retry_delay(3)] == [5.0, 15.0, 45.0]

    def test_jitter_bounds(self, monkeypatch):
        from rwmod.downloader import _retry_delay

        monkeypatch.setattr("rwmod.downloader.random.uniform", lambda a, b: 0.8)
        assert _retry_delay(1) == 4.0
        monkeypatch.setattr("rwmod.downloader.random.uniform", lambda a, b: 1.2)
        assert _retry_delay(2) == 18.0

    def test_download_one_sleeps_with_exponential_backoff(self, tmp_path: Path, monkeypatch):
        from rwmod.downloader import download_one
        from rwmod.steamcmd import DownloadResult, ErrorKind

        cfg = _cfg(tmp_path)
        calls: list[str] = []

        class _FailSteam:
            @property
            def workshop_content_dir(self) -> Path:
                return tmp_path / "nowhere"

            def workshop_download(self, mod_id: str) -> DownloadResult:
                calls.append(mod_id)
                return DownloadResult(False, mod_id, ErrorKind.UNKNOWN, "blip")

        sleeps: list[float] = []
        monkeypatch.setattr("rwmod.downloader.SteamCMD", lambda p: _FailSteam())
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr("rwmod.downloader.time.sleep", sleeps.append)
        monkeypatch.setattr("rwmod.downloader.random.uniform", lambda a, b: 1.0)

        assert download_one(cfg, "111") is False
        # First attempt + 3 retries, with the requested backoff between them.
        assert calls == ["111", "111", "111", "111"]
        assert sleeps == [5.0, 15.0, 45.0]


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
