"""Probe tests for the download / queue chain (DL-*).

Targets behaviour the existing suite does not cover: retry and
error-classification boundaries, incremental workshop_log parsing,
force-overwrite rollback in the batch path, per-mod locks, queue state machine
/ worker / restart recovery, batch concurrency, dependency BFS, auto-update
and offline state.

Convention: confirmed defects get a minimal repro marked
``xfail(strict=True, reason="DL-x: ...")`` — when the bug is fixed the strict
xfail flips to XPASS and fails, reminding the maintainer to drop the marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rwmod import offline as offline_mod
from rwmod import workshop as workshop_mod
from rwmod.autoupdate import AutoUpdateManager
from rwmod.config import Config, InstanceConfig
from rwmod.downloader import (
    _MOD_LOCKS_MAX,
    _auto_restore_backup,
    _collect_deps,
    _discard_stash,
    _download_one_unlocked,
    _find_existing,
    _find_existing_many,
    _install_content,
    _lock_for,
    _pick_folder_name,
    _rollback_stash,
    _skymods_fallback,
    download_batch,
    download_one,
)
from rwmod.queue import BATCH_SIZE, DownloadQueue, QueueItem
from rwmod.steamcmd import (
    DownloadResult,
    ErrorKind,
    SteamCMD,
    _live_procs,
    active_download_count,
    cancel_download,
)

# ── fixtures / helpers ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path):
    """Temp DB (real schema) + clean per-mod lock / live-proc registries."""
    from rwmod import database as db
    from rwmod import downloader as dl

    db.close_db()
    try:
        with patch("rwmod.database.DB_PATH", tmp_path / "probe_download.db"):
            db.init_db()
            yield
    finally:
        db.close_db()
        with dl._mod_locks_guard:
            dl._mod_locks.clear()
        _live_procs.clear()


def _cfg(tmp_path: Path) -> Config:
    exe = tmp_path / "steamcmd" / "steamcmd.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.touch()
    cfg = Config(steamcmd_path=exe, mods_dir=tmp_path / "Mods")
    cfg.mods_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir = tmp_path / "Backups"
    return cfg


def _install_mod(mods_dir: Path, folder: str, wid: str, pkg: str | None = None) -> Path:
    d = mods_dir / folder
    (d / "About").mkdir(parents=True, exist_ok=True)
    pkg = pkg or folder
    (d / "About" / "About.xml").write_text(
        f"<ModMetaData><name>{folder}</name><packageId>{pkg}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    (d / "About" / "PublishedFileId.txt").write_text(wid, encoding="utf-8")
    return d


def _workshop_content(cfg: Config, wid: str, pkg: str) -> Path:
    content = SteamCMD(cfg.steamcmd_path).workshop_content_dir / wid
    (content / "About").mkdir(parents=True, exist_ok=True)
    (content / "About" / "About.xml").write_text(
        f"<ModMetaData><packageId>{pkg}</packageId></ModMetaData>", encoding="utf-8"
    )
    (content / "About" / "PublishedFileId.txt").write_text(wid, encoding="utf-8")
    return content


def _res(ok: bool, mid: str, kind: str = ErrorKind.UNKNOWN, detail: str = "") -> DownloadResult:
    return DownloadResult(success=ok, mod_id=mid, error_kind=kind, error_detail=detail)


class _ScriptedSteam:
    """SteamCMD stand-in returning scripted DownloadResults per call."""

    def __init__(self, results: list[DownloadResult], content_dir: Path):
        self._results = list(results)
        self.calls: list[str] = []
        self.content_dir = Path(content_dir)

    @property
    def workshop_content_dir(self) -> Path:
        return self.content_dir

    def workshop_download(self, mod_id: str, instance_id: str | None = None) -> DownloadResult:
        self.calls.append(mod_id)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def _patch_steam(monkeypatch, cfg, results, skymods: bool = False) -> _ScriptedSteam:
    fake = _ScriptedSteam(results, SteamCMD(cfg.steamcmd_path).workshop_content_dir)
    monkeypatch.setattr("rwmod.downloader.SteamCMD", lambda path: fake)
    monkeypatch.setattr("rwmod.downloader.RETRY_DELAY", 0)
    monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
    monkeypatch.setattr("rwmod.downloader._skymods_fallback", lambda config, mid: skymods)
    return fake


class _StopLoop(Exception):
    """Sentinel used to break the auto-update background loop after one pass."""


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


# ── steamcmd: workshop_log boundaries ──────────────────────────────


@pytest.fixture
def sc(tmp_path: Path) -> SteamCMD:
    exe = tmp_path / "steamcmd.exe"
    exe.touch()
    return SteamCMD(exe)


def _write_log(sc: SteamCMD, text: str) -> Path:
    log = sc.workshop_log
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    return log


class TestWorkshopLogBoundaries:
    def test_offset_parses_only_appended_section(self, sc: SteamCMD):
        log = _write_log(sc, "[AppID 294100] Download item 12345 result : Failure\n")
        offset = log.stat().st_size
        with log.open("a", encoding="utf-8") as fh:
            fh.write("[AppID 294100] Download item 12345 result : OK\n")
        kind, _ = sc._parse_workshop_error("12345", offset)
        assert kind == ErrorKind.OK

    def test_pre_existing_failure_not_attributed_to_this_run(self, sc: SteamCMD):
        """The bytes before ``offset`` belong to earlier runs and must be ignored.

        A stale ``Failure`` for the same id in an earlier section must not be
        attributed to a run whose appended section has no line for the id.
        """
        log = _write_log(sc, "[AppID 294100] Download item 12345 result : Failure\n")
        offset = log.stat().st_size
        with log.open("a", encoding="utf-8") as fh:
            fh.write("[AppID 294100] Download item 99999 result : OK\n")
        kind, detail = sc._parse_workshop_error("12345", offset)
        # The stale Failure is ignored — but "no line in this run" means the
        # item was never processed, which is UNKNOWN, not OK.
        assert kind == ErrorKind.UNKNOWN
        assert "没有记录" in detail

    def test_offset_beyond_eof_after_truncation_is_safe(self, sc: SteamCMD):
        _write_log(sc, "[AppID 294100] Download item 12345 result : Failure\n")
        kind, detail = sc._parse_workshop_error("12345", offset=10_000)
        assert kind == ErrorKind.UNKNOWN
        assert "没有记录" in detail

    def test_empty_log_is_unknown(self, sc: SteamCMD):
        _write_log(sc, "")
        kind, detail = sc._parse_workshop_error("12345", 0)
        # The old default-OK turned every unlogged item into a bogus
        # "success but no content" failure downstream.
        assert kind == ErrorKind.UNKNOWN
        assert "没有记录" in detail

    def test_log_offset_snapshot(self, sc: SteamCMD):
        assert sc._log_offset() == 0
        _write_log(sc, "abc")
        assert sc._log_offset() == 3

    def test_detail_line_overrides_generic_result(self, sc: SteamCMD):
        _write_log(
            sc,
            "[AppID 294100] Get details for item 12345 failed : Wrong AppID 241100\n"
            "[AppID 294100] Download item 12345 result : No Match\n",
        )
        kind, detail = sc._parse_workshop_error("12345")
        assert kind == ErrorKind.NO_MATCH
        assert "Wrong AppID 241100" in detail

    def test_specific_detail_replaces_generic_result(self, sc: SteamCMD):
        _write_log(
            sc,
            "[AppID 294100] Get details for item 12345 failed : File Not Found\n"
            "[AppID 294100] Download item 12345 result : Failure\n",
        )
        kind, detail = sc._parse_workshop_error("12345")
        assert kind == ErrorKind.FAILURE
        # The more specific "Get details" line supplies the human detail.
        assert detail == "SteamCMD: File Not Found"

    def test_detail_only_no_result_is_unknown(self, sc: SteamCMD):
        _write_log(sc, "[AppID 294100] Get details for item 12345 failed : Boom\n")
        kind, detail = sc._parse_workshop_error("12345")
        assert kind == ErrorKind.UNKNOWN
        assert detail == "SteamCMD: Boom"


class TestWorkshopDownloadManyProcess:
    def _proc(self, out: str = "") -> MagicMock:
        proc = MagicMock()
        proc.wait.return_value = 0
        proc.stdout = io.StringIO(out)
        return proc

    def test_one_process_for_whole_batch(self, sc: SteamCMD):
        content = sc.workshop_content_dir / "111"
        content.mkdir(parents=True)
        _write_log(sc, "[AppID 294100] Download item 111 result : OK\n")
        proc = self._proc()
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc) as mock_popen:
            results = sc.workshop_download_many(["111"])
        assert mock_popen.call_count == 1
        cmd = mock_popen.call_args.args[0]
        assert cmd.count("+login") == 1
        assert cmd.count("+workshop_download_item") == 1
        assert results["111"].success

    def test_timeout_marks_every_id_and_frees_registry(self, sc: SteamCMD):
        _live_procs.clear()
        proc = MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="steamcmd", timeout=1)
        proc.stdout = io.StringIO("")
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc):
            results = sc.workshop_download_many(["111", "222"])
        assert {r.error_kind for r in results.values()} == {ErrorKind.TIMEOUT}
        assert _live_procs == {}
        assert proc.kill.called

    def test_progress_ignored_for_id_outside_batch(self, sc: SteamCMD):
        out = (
            "Downloading item 999 ...\nUpdate state (0x61) downloading, progress: 50.00 (5 / 10)\n"
        )
        proc = self._proc(out)
        seen: list[tuple] = []
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc):
            sc.workshop_download_many(["111"], progress_cb=lambda *a: seen.append(a))
        assert seen == []

    def test_progress_callback_exception_is_swallowed(self, sc: SteamCMD):
        content = sc.workshop_content_dir / "111"
        content.mkdir(parents=True)
        _write_log(sc, "[AppID 294100] Download item 111 result : OK\n")
        out = (
            "Downloading item 111 ...\nUpdate state (0x61) downloading, progress: 50.00 (5 / 10)\n"
        )
        proc = self._proc(out)

        def bad_cb(*args: object) -> None:
            raise RuntimeError("callback exploded")

        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc):
            results = sc.workshop_download_many(["111"], progress_cb=bad_cb)
        assert results["111"].success

    def test_active_download_count_tracks_registry(self):
        _live_procs.clear()
        assert active_download_count() == 0
        _live_procs[("", "1")] = MagicMock()
        _live_procs[("", "2")] = MagicMock()
        assert active_download_count() == 2
        assert cancel_download(None, "1") is True
        assert active_download_count() == 1


# ── downloader: install / locks / deps ─────────────────────────────


class TestInstallContentEdges:
    def test_existing_dest_without_force_is_kept(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")
        dest = _install_mod(cfg.mods_dir, "author.mod", "999", pkg="author.mod")
        marker = dest / "About" / "marker.txt"
        marker.write_text("old", encoding="utf-8")
        assert _install_content(cfg, "111", force=False) is True
        assert marker.exists()

    def test_force_removes_dest_before_copy(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")
        dest = _install_mod(cfg.mods_dir, "author.mod", "999", pkg="author.mod")
        (dest / "About" / "marker.txt").write_text("old", encoding="utf-8")
        assert _install_content(cfg, "111", force=True) is True
        assert not (dest / "About" / "marker.txt").exists()
        assert (dest / "About" / "About.xml").is_file()

    def test_copytree_failure_returns_false(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("rwmod.downloader.shutil.copytree", boom)
        assert _install_content(cfg, "111") is False


class TestPerModLocks:
    def test_same_id_returns_same_lock(self):
        assert _lock_for("same") is _lock_for("same")
        assert _lock_for("other") is not _lock_for("same")

    def test_eviction_never_drops_held_lock(self):
        held = _lock_for("held")
        held.acquire()
        try:
            for i in range(_MOD_LOCKS_MAX + 20):
                _lock_for(f"probe-{i}")
            assert _lock_for("held") is held
        finally:
            held.release()

    def test_concurrent_same_mod_downloads_are_serialized(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        state = {"calls": 0, "cur": 0, "max": 0}
        guard = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        class FakeSteam:
            def __init__(self, path: object) -> None:
                pass

            @property
            def workshop_content_dir(self) -> Path:
                return tmp_path / "no-content"

            def workshop_download(
                self, mod_id: str, instance_id: str | None = None
            ) -> DownloadResult:
                with guard:
                    state["calls"] += 1
                    state["cur"] += 1
                    state["max"] = max(state["max"], state["cur"])
                entered.set()
                release.wait(5)
                with guard:
                    state["cur"] -= 1
                return _res(False, mod_id, ErrorKind.FILE_NOT_FOUND, "gone")

        monkeypatch.setattr("rwmod.downloader.SteamCMD", FakeSteam)
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr("rwmod.downloader._skymods_fallback", lambda config, mid: False)

        results: list[bool] = []
        t1 = threading.Thread(target=lambda: results.append(download_one(cfg, "123")))
        t2 = threading.Thread(target=lambda: results.append(download_one(cfg, "123")))
        t1.start()
        assert entered.wait(5)
        t2.start()
        time.sleep(0.3)  # t2 must be blocked on the per-mod lock here
        with guard:
            calls_when_first_held = state["calls"]
        release.set()
        t1.join(5)
        t2.join(5)
        assert calls_when_first_held == 1
        assert state["max"] == 1  # the two downloads never overlapped
        assert sorted(results) == [False, False]

    def test_lock_released_when_core_download_raises(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)

        def boom(*args: object, **kwargs: object) -> bool:
            raise RuntimeError("core exploded")

        monkeypatch.setattr("rwmod.downloader._download_one_unlocked", boom)
        with pytest.raises(RuntimeError):
            download_one(cfg, "123")

        monkeypatch.setattr("rwmod.downloader._download_one_unlocked", lambda *a, **k: True)
        done = threading.Event()
        out: list[bool] = []
        worker = threading.Thread(
            target=lambda: (out.append(download_one(cfg, "123")), done.set()), daemon=True
        )
        worker.start()
        assert done.wait(3), "per-mod lock was not released after an exception"
        assert out == [True]


class TestDependencyBFS:
    def _graph(self, monkeypatch, graph: dict[str, list[str]], calls: list[list[str]]):
        def fake_deps(mod_ids: list[str]) -> dict[str, list[str]]:
            calls.append(list(mod_ids))
            return {mid: graph[mid] for mid in mod_ids if graph.get(mid)}

        monkeypatch.setattr("rwmod.workshop.fetch_item_dependencies", fake_deps)

    def test_transitive_dependencies_collected(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        calls: list[list[str]] = []
        self._graph(monkeypatch, {"A": ["B"], "B": ["C"], "C": []}, calls)
        deferred: list[str] = []
        _collect_deps(cfg, "A", deferred)
        assert deferred == ["B", "C"]
        assert calls == [["A"], ["B"], ["C"]]

    def test_cyclic_dependency_terminates(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        calls: list[list[str]] = []
        self._graph(monkeypatch, {"A": ["B"], "B": ["A"]}, calls)
        deferred: list[str] = []
        _collect_deps(cfg, "A", deferred)
        assert deferred == ["B"]

    def test_installed_dependency_is_skipped(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "SomeDep", "500", pkg="dep.one")
        calls: list[list[str]] = []
        self._graph(monkeypatch, {"100": ["500", "600"], "600": []}, calls)
        deferred: list[str] = []
        _collect_deps(cfg, "100", deferred)
        assert deferred == ["600"]

    def test_none_collector_traverses_but_downloads_nothing(self, tmp_path: Path, monkeypatch):
        """deferred=None (direct _download_one_unlocked callers) must not crash."""
        cfg = _cfg(tmp_path)
        calls: list[list[str]] = []
        self._graph(monkeypatch, {"A": ["B"], "B": []}, calls)
        _collect_deps(cfg, "A", None)
        assert calls == [["A"], ["B"]]

    def test_api_failure_is_swallowed(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)

        def boom(mod_ids: list[str]) -> dict[str, list[str]]:
            raise RuntimeError("api down")

        monkeypatch.setattr("rwmod.workshop.fetch_item_dependencies", boom)
        _collect_deps(cfg, "A", [])  # must not raise


class TestFindExistingFallbacks:
    def test_scan_repairs_metadata_index(self, tmp_path: Path):
        from rwmod.database import find_local_mods_by_workshop_id

        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "ModA", "111", pkg="a.mod")
        (cfg.mods_dir / "stray.txt").write_text("x", encoding="utf-8")
        (cfg.mods_dir / "NoAboutFolder").mkdir()
        assert find_local_mods_by_workshop_id("111") == []
        found = _find_existing(cfg.mods_dir, "111")
        assert found is not None and found.name == "ModA"
        assert find_local_mods_by_workshop_id("111") == ["ModA"]

    def test_db_error_falls_back_to_scan(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "ModA", "111", pkg="a.mod")

        def boom(mod_id: str) -> list[str]:
            raise RuntimeError("db corrupt")

        monkeypatch.setattr("rwmod.downloader.find_local_mods_by_workshop_id", boom)
        found = _find_existing(cfg.mods_dir, "111")
        assert found is not None and found.name == "ModA"

    def test_find_existing_many_db_error_scans_all(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "ModA", "111", pkg="a.mod")

        def boom(mod_ids: list[str]) -> dict[str, list[str]]:
            raise RuntimeError("db corrupt")

        monkeypatch.setattr("rwmod.downloader.find_local_mods_by_workshop_ids", boom)
        result = _find_existing_many(cfg.mods_dir, ["111", "222"])
        assert result["111"] is not None and result["111"].name == "ModA"
        assert result["222"] is None

    def test_missing_mods_dir_returns_none(self, tmp_path: Path):
        assert _find_existing(tmp_path / "nope", "111") is None
        assert _find_existing_many(tmp_path / "nope", ["111"]) == {"111": None}

    def test_malformed_about_xml_falls_back_to_mod_id(self, tmp_path: Path):
        d = tmp_path / "m"
        (d / "About").mkdir(parents=True)
        (d / "About" / "About.xml").write_text("<ModMetaData><name>truncated", encoding="utf-8")
        assert _pick_folder_name(d, "12345") == "mod_12345"


# ── downloader: retry / skymods / collections ──────────────────────


class TestDownloadOneRetry:
    def test_transient_failures_retry_then_succeed(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")
        fake = _patch_steam(
            monkeypatch, cfg, [_res(False, "111"), _res(False, "111"), _res(True, "111")]
        )
        assert download_one(cfg, "111") is True
        assert fake.calls == ["111", "111", "111"]

    def test_non_retryable_stops_immediately_and_skips_skymods(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        fake = _patch_steam(
            monkeypatch, cfg, [_res(False, "111", ErrorKind.FILE_NOT_FOUND, "gone")]
        )
        skymods_calls: list[str] = []
        monkeypatch.setattr(
            "rwmod.downloader._skymods_fallback",
            lambda config, mid: skymods_calls.append(mid) or False,
        )
        assert download_one(cfg, "111") is False
        assert fake.calls == ["111"]  # one attempt only
        assert skymods_calls == []  # non-retryable → no fallback

    def test_exhausted_retries_then_skymods_success_discards_stash(
        self, tmp_path: Path, monkeypatch
    ):
        cfg = _cfg(tmp_path)
        cfg.enable_skymods = True  # opt-in since P2-2
        _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        fake = _patch_steam(monkeypatch, cfg, [_res(False, "111")], skymods=True)
        assert download_one(cfg, "111", force=True) is True
        # First attempt + MAX_RETRIES retries (5s → 15s → 45s backoff).
        assert fake.calls == ["111", "111", "111", "111"]
        assert not list(cfg.mods_dir.glob(".rwmod_stash_*"))

    def test_exhausted_retries_without_skymods_returns_false(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _patch_steam(monkeypatch, cfg, [_res(False, "111")])
        assert download_one(cfg, "111") is False


class TestDownloadOneCollections:
    def test_children_deferred_and_downloaded_outside_lock(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        fake = _patch_steam(
            monkeypatch,
            cfg,
            [
                _res(False, "100", ErrorKind.FILE_NOT_FOUND, "gone"),
                _res(False, "200", ErrorKind.FILE_NOT_FOUND, "gone"),
            ],
        )
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: mid == "999")
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: ["100", "200"])
        assert download_one(cfg, "999") is True
        assert fake.calls == ["100", "200"]

    def test_empty_collection_returns_false(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: True)
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: [])
        assert download_one(cfg, "999") is False

    def test_collection_probe_error_fails_without_steamcmd(self, tmp_path: Path, monkeypatch):
        """K-2: probe failure must not degrade to a SteamCMD download.

        A collection id handed to SteamCMD "downloads" without an About.xml,
        then the Skymods fallback gets a 403 — the exact failure chain seen on
        2026-10-18. Fail explicitly instead.
        """
        cfg = _cfg(tmp_path)

        fake = _patch_steam(
            monkeypatch, cfg, [_res(False, "999", ErrorKind.FILE_NOT_FOUND, "gone")]
        )

        def boom(mod_id: str) -> bool:
            raise RuntimeError("api down")

        monkeypatch.setattr("rwmod.workshop.is_collection", boom)
        assert download_one(cfg, "999") is False
        assert fake.calls == []

    def test_probe_error_during_force_update_keeps_old_mod(self, tmp_path: Path, monkeypatch):
        """A failed probe must not leave a force-updated mod stashed aside.

        The force-overwrite stash runs before collection detection, so the
        fail-closed return has to put the old copy back — otherwise a network
        blip during an update removes the mod from the game.
        """
        cfg = _cfg(tmp_path)
        old = _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")

        def boom(mod_id: str) -> bool:
            raise RuntimeError("api down")

        # _patch_steam() pins is_collection=False; install the failing probe
        # afterwards so it wins.
        fake = _patch_steam(monkeypatch, cfg, [])  # any SteamCMD call would fail
        monkeypatch.setattr("rwmod.workshop.is_collection", boom)

        assert download_one(cfg, "111", force=True) is False
        assert old.is_dir()
        assert not list(cfg.mods_dir.glob(".rwmod_stash_*"))
        assert fake.calls == []

    def test_no_collector_recursive_path(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "ChildOne", "100", pkg="a.one")
        _install_mod(cfg.mods_dir, "ChildTwo", "200", pkg="a.two")
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: mid == "999")
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: ["100", "200"])
        assert _download_one_unlocked(cfg, "999", False, None) is True


class TestSkymodsCircuitBreaker:
    """P2-2: opt-in fallback + 10-minute breaker after the first failure."""

    @pytest.fixture(autouse=True)
    def _reset_breaker(self):
        from rwmod import skymods

        skymods.reset_skymods_breaker()
        yield
        skymods.reset_skymods_breaker()

    def test_disabled_by_default_never_touches_the_network(self, tmp_path: Path, monkeypatch):
        from rwmod.skymods import try_skymods

        cfg = _cfg(tmp_path)
        calls: list[int] = []
        monkeypatch.setattr("rwmod.skymods.urllib.request.urlopen", lambda *a, **k: calls.append(1))
        assert try_skymods("111", cfg) is None
        assert calls == []

    def test_first_failure_trips_breaker_and_logs_once(self, tmp_path: Path, monkeypatch, caplog):
        from rwmod import skymods

        cfg = _cfg(tmp_path)
        cfg.enable_skymods = True
        calls: list[int] = []

        def boom(req, timeout=20):
            calls.append(1)
            raise OSError("HTTP Error 403: Forbidden")

        monkeypatch.setattr("rwmod.skymods.urllib.request.urlopen", boom)
        with caplog.at_level("WARNING", logger="rwmod.skymods"):
            assert skymods.try_skymods("111", cfg) is None
            assert skymods.try_skymods("222", cfg) is None

        assert len(calls) == 1  # second call short-circuits on the open breaker
        warnings = [
            r for r in caplog.records if r.name == "rwmod.skymods" and r.levelname == "WARNING"
        ]
        assert len(warnings) == 1  # one log line per breaker window
        assert "Skymods" in warnings[0].getMessage()

    def test_breaker_expires_and_allows_a_retry(self, tmp_path: Path, monkeypatch):
        from rwmod import skymods

        cfg = _cfg(tmp_path)
        cfg.enable_skymods = True
        calls: list[int] = []

        def boom(req, timeout=20):
            calls.append(1)
            raise OSError("still blocked")

        monkeypatch.setattr("rwmod.skymods.urllib.request.urlopen", boom)
        assert skymods.try_skymods("111", cfg) is None
        assert len(calls) == 1

        later = skymods.time.monotonic() + skymods.SKYMODS_BLOCK_SECONDS + 1
        monkeypatch.setattr(skymods.time, "monotonic", lambda: later)
        assert skymods.skymods_status(cfg) == "available"
        assert skymods.try_skymods("222", cfg) is None
        assert len(calls) == 2

    def test_status_reports_disabled_blocked_available(self, tmp_path: Path):
        from rwmod import skymods

        cfg = _cfg(tmp_path)
        assert skymods.skymods_status(cfg) == "disabled"
        cfg.enable_skymods = True
        assert skymods.skymods_status(cfg) == "available"
        skymods._mark_skymods_blocked("test")
        assert skymods.skymods_status(cfg) == "blocked"


class TestUpdateHardening:
    """P1-2: persistent backup + post-install verification + rollback order."""

    def test_verify_install_accepts_a_valid_copy(self, tmp_path: Path):
        from rwmod.downloader import _verify_install

        dest = tmp_path / "mod"
        (dest / "About").mkdir(parents=True)
        (dest / "About" / "About.xml").write_text("<ModMetaData><packageId>a.b</packageId>")
        (dest / "About" / "PublishedFileId.txt").write_text("111")
        (dest / "Defs").mkdir()
        (dest / "Defs" / "a.xml").write_text("<Defs/>")
        assert _verify_install(dest, "111") == (True, "")

    def test_verify_install_rejects_missing_about(self, tmp_path: Path):
        from rwmod.downloader import _verify_install

        dest = tmp_path / "mod"
        (dest / "About").mkdir(parents=True)
        (dest / "About" / "PublishedFileId.txt").write_text("111")
        ok, reason = _verify_install(dest, "111")
        assert ok is False and "About.xml" in reason

    def test_verify_install_rejects_wrong_published_file_id(self, tmp_path: Path):
        from rwmod.downloader import _verify_install

        dest = tmp_path / "mod"
        (dest / "About").mkdir(parents=True)
        (dest / "About" / "About.xml").write_text("<ModMetaData/>")
        (dest / "About" / "PublishedFileId.txt").write_text("999")
        ok, reason = _verify_install(dest, "111")
        assert ok is False and "PublishedFileId" in reason

    def test_verify_install_rejects_empty_about(self, tmp_path: Path):
        from rwmod.downloader import _verify_install

        dest = tmp_path / "mod"
        (dest / "About").mkdir(parents=True)
        (dest / "About" / "About.xml").write_text("")
        (dest / "About" / "PublishedFileId.txt").write_text("111")
        ok, reason = _verify_install(dest, "111")
        assert ok is False and "空" in reason

    def test_force_update_creates_persistent_backup(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        _workshop_content(cfg, "111", "author.mod")
        _patch_steam(monkeypatch, cfg, [_res(True, "111")])

        assert download_one(cfg, "111", force=True) is True
        assert len(list(cfg.backup_dir.glob("111__*.zip"))) == 1

    def test_update_backup_can_be_disabled(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.update_backup = False
        _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        _workshop_content(cfg, "111", "author.mod")
        _patch_steam(monkeypatch, cfg, [_res(True, "111")])

        assert download_one(cfg, "111", force=True) is True
        assert not list(cfg.backup_dir.glob("111__*.zip"))

    def test_verification_failure_rolls_back_and_keeps_old_mod(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        old = _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        content = _workshop_content(cfg, "111", "author.mod")
        # The downloaded item claims to be a different workshop id.
        (content / "About" / "PublishedFileId.txt").write_text("999")
        _patch_steam(monkeypatch, cfg, [_res(True, "111")])

        assert download_one(cfg, "111", force=True) is False
        assert old.is_dir()
        assert "author.mod" in (old / "About" / "About.xml").read_text()
        assert not list(cfg.mods_dir.glob(".rwmod_stash_*"))
        # The pre-overwrite backup is still available for manual recovery.
        assert len(list(cfg.backup_dir.glob("111__*.zip"))) == 1

    def test_last_updated_written_only_after_verification(self, tmp_path: Path, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(
            "rwmod.database.set_last_updated",
            lambda folder, ts, **kwargs: calls.append(folder),
        )

        # Failure case: verification rejects the copy → no timestamp.
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        content = _workshop_content(cfg, "111", "author.mod")
        (content / "About" / "PublishedFileId.txt").write_text("999")
        _patch_steam(monkeypatch, cfg, [_res(True, "111")])
        assert download_one(cfg, "111", force=True) is False
        assert calls == []

        # Success case: timestamp written exactly once.
        cfg2 = _cfg(tmp_path / "second")
        _install_mod(cfg2.mods_dir, "author.mod", "111", pkg="author.mod")
        _workshop_content(cfg2, "111", "author.mod")
        _patch_steam(monkeypatch, cfg2, [_res(True, "111")])
        assert download_one(cfg2, "111", force=True) is True
        assert calls == ["author.mod"]

    def test_successful_update_prunes_backups_to_keep_limit(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.backup_keep = 3
        _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        _workshop_content(cfg, "111", "author.mod")
        cfg.backup_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            p = cfg.backup_dir / f"111__old__20260101_00000{i}.zip"
            p.write_bytes(b"x")
            os.utime(p, (1000 + i, 1000 + i))
        _patch_steam(monkeypatch, cfg, [_res(True, "111")])

        assert download_one(cfg, "111", force=True) is True
        assert len(list(cfg.backup_dir.glob("111__*.zip"))) == 3

    def test_rollback_uses_the_backup_zip_when_stash_is_gone(self, tmp_path: Path):
        """Rollback order is stash → the specific backup taken before the update."""
        from rwmod.backup import backup_mod
        from rwmod.downloader import _rollback_failed_install

        cfg = _cfg(tmp_path)
        old = _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        backup_path = backup_mod(cfg.mods_dir, "111", old.name, cfg.backup_dir)
        assert backup_path is not None
        shutil.rmtree(old)  # simulate a lost stash + missing destination

        assert _rollback_failed_install(cfg, "111", old, None, backup_path) is True
        assert (old / "About" / "About.xml").exists()


class TestStashRollbackUnits:
    def test_rollback_missing_stash_returns_false(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        assert _rollback_stash(cfg, "111", None) is False
        assert _rollback_stash(cfg, "111", cfg.mods_dir / ".rwmod_stash_gone_1") is False

    def test_rollback_discards_stash_when_dest_exists(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        stash = cfg.mods_dir / ".rwmod_stash_OldMod_1700000000"
        stash.mkdir()
        dest = _install_mod(cfg.mods_dir, "OldMod", "111", pkg="old.mod")
        assert _rollback_stash(cfg, "111", stash) is True
        assert not stash.exists()
        assert dest.exists()

    def test_rollback_rename_failure_returns_false(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        stash = cfg.mods_dir / ".rwmod_stash_OldMod_1700000000"
        stash.mkdir()

        def boom(self: Path, target: object) -> None:
            raise OSError("locked")

        monkeypatch.setattr(Path, "rename", boom)
        assert _rollback_stash(cfg, "111", stash) is False

    def test_discard_stash_is_noop_for_none_and_missing(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        _discard_stash(None)
        _discard_stash(cfg.mods_dir / ".rwmod_stash_gone_1")

    def test_auto_restore_backup_outcomes(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.backup.restore_mod", lambda *a, **k: {"ok": True})
        _auto_restore_backup(cfg, "111")
        monkeypatch.setattr("rwmod.backup.restore_mod", lambda *a, **k: {"ok": False, "msg": "no"})
        _auto_restore_backup(cfg, "111")

        def boom(*args: object, **kwargs: object) -> dict:
            raise RuntimeError("backup broken")

        monkeypatch.setattr("rwmod.backup.restore_mod", boom)
        _auto_restore_backup(cfg, "111")  # must be swallowed

    def test_skymods_wrapper_reports_both_outcomes(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.enable_skymods = True  # opt-in since P2-2
        monkeypatch.setattr("rwmod.skymods.try_skymods", lambda mid, config: cfg.mods_dir / "hit")
        assert _skymods_fallback(cfg, "111") is True
        monkeypatch.setattr("rwmod.skymods.try_skymods", lambda mid, config: None)
        assert _skymods_fallback(cfg, "111") is False


class TestDownloadBatchExtras:
    def test_duplicate_ids_deduplicated(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        seen: list[list[str]] = []

        def fake_many(self, ids, progress_cb=None, instance_id=None):
            seen.append(list(ids))
            return {m: _res(False, m, ErrorKind.FILE_NOT_FOUND, "gone") for m in ids}

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_many)
        result = download_batch(cfg, ["111", "111"])
        assert seen == [["111"]]
        assert result == {"111": False}

    def test_install_failure_falls_back_to_skymods(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None, instance_id=None: {m: _res(True, m) for m in ids},
        )

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("rwmod.downloader.shutil.copytree", boom)
        monkeypatch.setattr("rwmod.downloader._skymods_fallback", lambda config, mid: True)
        assert download_batch(cfg, ["111"]) == {"111": True}

    def test_collection_without_collector_downloads_children_recursively(
        self, tmp_path: Path, monkeypatch
    ):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: mid == "999")
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: ["100", "200"])
        seen: list[str] = []
        monkeypatch.setattr(
            "rwmod.downloader.download_one",
            lambda config, mid, force=False: seen.append(mid) or True,
        )
        result = download_batch(cfg, ["999"])
        assert result["999"] is True
        assert seen == ["100", "200"]

    def test_cancelled_batch_does_not_retry_or_reclassify(self, tmp_path: Path, monkeypatch):
        """P1-1: every id of a killed batch reports CANCELLED and is not retried."""
        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None, instance_id=None: {
                m: _res(False, m, ErrorKind.CANCELLED, "SteamCMD 批次被取消") for m in ids
            },
        )
        retried: list[str] = []
        monkeypatch.setattr(
            "rwmod.downloader.download_one",
            lambda *args, **kwargs: retried.append(args[1]) or False,
        )

        outcomes: dict = {}
        result = download_batch(cfg, ["111", "222"], outcomes=outcomes)
        assert result == {"111": False, "222": False}
        assert retried == []  # no immediate re-download of the cancelled batch
        assert outcomes["111"].error_kind == ErrorKind.CANCELLED
        assert outcomes["222"].error_kind == ErrorKind.CANCELLED

    def test_completed_sibling_of_cancelled_batch_is_installed(self, tmp_path: Path, monkeypatch):
        """A sibling SteamCMD had already finished installs normally."""
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "222", "author.two")
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None, instance_id=None: {
                "111": _res(False, "111", ErrorKind.CANCELLED, "cancelled"),
                "222": _res(True, "222"),
            },
        )

        outcomes: dict = {}
        result = download_batch(cfg, ["111", "222"], outcomes=outcomes)
        assert result == {"111": False, "222": True}
        assert outcomes["222"].ok is True
        assert (cfg.mods_dir / "author.two").is_dir()

    def test_collection_probe_error_fails_without_steamcmd_in_batch(
        self, tmp_path: Path, monkeypatch
    ):
        """K-2: a broken probe fails the id; it must not fall back to SteamCMD."""
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")
        steamcmd_calls: list[list[str]] = []

        def boom(mod_id: str) -> bool:
            raise RuntimeError("api down")

        def fake_many(self, ids, progress_cb=None, instance_id=None):
            steamcmd_calls.append(list(ids))
            return {m: _res(True, m) for m in ids}

        monkeypatch.setattr("rwmod.workshop.is_collection", boom)
        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_many)
        assert download_batch(cfg, ["111"]) == {"111": False}
        assert steamcmd_calls == []
        assert not (cfg.mods_dir / "author.mod").is_dir()


# ── queue: state machine / worker / persistence ────────────────────


class TestQueueStateMachine:
    def test_process_batch_skips_installed_mod(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "ModA", "111", pkg="a.mod")
        q = DownloadQueue()
        item = QueueItem(id="111")
        q.items.append(item)
        with patch("rwmod.queue.download_batch") as mock_dl:
            asyncio.run(q._process_batch(cfg, [item], force=False))
        mock_dl.assert_not_called()
        assert (item.status, item.progress, item.name, item.msg) == ("done", 1.0, "ModA", "已安装")

    def test_process_batch_force_downloads_installed_mod(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "ModA", "111", pkg="a.mod")
        q = DownloadQueue()
        item = QueueItem(id="111")
        q.items.append(item)
        calls: list[dict] = []

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            calls.append({"ids": list(ids), "force": force})
            return {mid: True for mid in ids}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)
        asyncio.run(q._process_batch(cfg, [item], force=True))
        assert calls == [{"ids": ["111"], "force": True}]
        assert item.status == "done" and item.name == "ModA"

    def test_process_batch_failure_marks_failed(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        item = QueueItem(id="111")
        q.items.append(item)
        monkeypatch.setattr(
            "rwmod.queue.download_batch",
            lambda config, ids, force=False, extra_out=None, progress_cb=None: {"111": False},
        )
        asyncio.run(q._process_batch(cfg, [item], force=False))
        assert (item.status, item.progress) == ("failed", 0)

    def test_process_batch_uses_id_when_folder_unknown(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        item = QueueItem(id="111")
        q.items.append(item)
        monkeypatch.setattr(
            "rwmod.queue.download_batch",
            lambda config, ids, force=False, extra_out=None, progress_cb=None: {"111": True},
        )
        asyncio.run(q._process_batch(cfg, [item], force=False))
        assert item.status == "done" and item.name == "111"

    def test_process_batch_requeues_extra_ids(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        item = QueueItem(id="111")
        q.items.append(item)

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            if extra_out is not None:
                extra_out.append("999")
            return {mid: True for mid in ids}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)
        asyncio.run(q._process_batch(cfg, [item], force=False))
        assert any(i.id == "999" and i.status == "pending" for i in q.items)

    def test_progress_callback_updates_item_telemetry(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        item = QueueItem(id="111")
        q.items.append(item)
        clock = iter([1000.0, 1002.0])
        monkeypatch.setattr("rwmod.queue.time", SimpleNamespace(monotonic=lambda: next(clock)))
        observed: dict[str, float | str] = {}

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            progress_cb("111", 42.0, 4200.0, 10000.0)
            progress_cb("111", 84.0, 8400.0, 10000.0)
            observed["downloaded"] = item.downloaded
            observed["total"] = item.total
            observed["speed"] = item.speed_bps
            observed["progress"] = item.progress
            observed["msg"] = item.msg
            return {"111": True}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)

        async def scenario() -> None:
            q._loop = asyncio.get_running_loop()
            await q._process_batch(cfg, [item], force=False)

        asyncio.run(scenario())
        assert observed["downloaded"] == 8400.0
        assert observed["total"] == 10000.0
        assert observed["speed"] == pytest.approx(2100.0)  # (8400-4200) / 2.0s
        assert observed["progress"] == 0.84  # capped at 0.95 while downloading
        assert observed["msg"] == "下载中 84.0%"
        assert item.status == "done" and item.progress == 1.0  # install completes


class TestQueueWorker:
    def test_worker_picks_up_item_added_while_running(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        seen: list[list[str]] = []
        lock = threading.Lock()

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            with lock:
                seen.append(list(ids))
            time.sleep(0.1)
            return {mid: True for mid in ids}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)
        q.add(["111"])

        async def scenario() -> list[str]:
            await q.start(cfg)
            await asyncio.sleep(0.03)  # first batch is now in flight
            q.add(["222"])
            for _ in range(300):
                await asyncio.sleep(0.02)
                if all(i.status == "done" for i in q.items):
                    break
            q.stop()
            # DL-8: wait_stopped currently re-raises CancelledError from the
            # worker task it just cancelled; shutdown must still proceed.
            with contextlib.suppress(asyncio.CancelledError):
                await q.wait_stopped(2)
            return [i.id for i in q.items]

        ids = asyncio.run(asyncio.wait_for(scenario(), timeout=15))
        expected = ["111", "222"]
        assert sorted(ids) == expected
        assert sorted(mid for batch in seen for mid in batch) == expected

    def test_worker_crash_does_not_strand_item(self, tmp_path: Path, monkeypatch):
        # DL-3 fixed: the worker's crash fallback now rescues items already
        # flipped to 'downloading' too, so a batch exception cannot strand them.
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        q._config = cfg
        item = QueueItem(id="111")
        q.items.append(item)

        def boom(*args: object, **kwargs: object) -> dict:
            raise RuntimeError("batch exploded")

        monkeypatch.setattr("rwmod.queue.download_batch", boom)

        async def scenario() -> None:
            task = asyncio.create_task(q._worker_loop())
            for _ in range(120):
                await asyncio.sleep(0.01)
                if item.status in ("failed", "done", "cancelled", "pending"):
                    break
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert item.status == "failed"

    def test_two_batches_run_concurrently(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        state = {"cur": 0, "max": 0, "calls": 0}
        guard = threading.Lock()

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            with guard:
                state["cur"] += 1
                state["calls"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.25)
            with guard:
                state["cur"] -= 1
            return {mid: True for mid in ids}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)
        q.add([str(1000 + i) for i in range(BATCH_SIZE * 2)])

        async def scenario() -> None:
            await q.start(cfg)
            for _ in range(400):
                await asyncio.sleep(0.02)
                if all(i.status == "done" for i in q.items):
                    break
            q.stop()
            with contextlib.suppress(asyncio.CancelledError):  # see DL-8
                await q.wait_stopped(2)

        asyncio.run(asyncio.wait_for(scenario(), timeout=15))
        assert state["calls"] == 2
        assert state["max"] >= 2

    def test_start_reuses_worker_and_stop_waits(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()

        async def scenario() -> asyncio.Task:
            await q.start(cfg)
            worker = q._worker
            assert worker is not None
            await q.start(cfg)
            assert q._worker is worker
            q.stop()
            with contextlib.suppress(asyncio.CancelledError):  # see DL-8
                await q.wait_stopped(2)
            return worker

        worker = asyncio.run(scenario())
        assert worker.done()
        assert q._running is False

    def test_wait_stopped_without_worker_returns(self):
        q = DownloadQueue()
        asyncio.run(q.wait_stopped(0.1))  # must not raise

    def test_wait_stopped_returns_normally_after_stop(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()

        async def scenario() -> None:
            await q.start(cfg)
            q.stop()
            await q.wait_stopped(2)  # must return, not raise CancelledError

        asyncio.run(scenario())


class TestQueuePersistence:
    def test_remove_pending_deletes_db_row(self):
        from rwmod.database import queue_load_all

        q = DownloadQueue()
        q.add(["111"])
        assert q.remove("111") is True
        assert queue_load_all() == []

    def test_remove_downloading_persists_cancelled(self):
        from rwmod.database import queue_load_all

        q = DownloadQueue()
        q.add(["111"])
        q.items[0].status = "downloading"
        assert q.remove("111") is True
        row = queue_load_all()[0]
        assert row["status"] == "cancelled"
        assert q.items[0].status == "cancelled"

    def test_remove_unknown_id_returns_false(self):
        assert DownloadQueue().remove("nope") is False

    def test_clear_done_clears_memory_and_db(self):
        from rwmod.database import queue_load_all

        q = DownloadQueue()
        q.add(["1", "2", "3", "4"])
        q.items[0].status = "done"
        q.items[1].status = "cancelled"
        q.items[2].status = "failed"
        q.items[3].status = "pending"
        for item in q.items:
            q._persist(item)
        q.clear_done()
        assert sorted(i.id for i in q.items) == ["3", "4"]
        assert sorted(r["workshop_id"] for r in queue_load_all()) == ["3", "4"]

    def test_load_from_db_restores_pending_item(self):
        from rwmod.database import queue_upsert

        queue_upsert("111", name="Restored", status="pending", progress=0.25, msg="queued")
        q = DownloadQueue()
        q._load_from_db()
        assert len(q.items) == 1
        item = q.items[0]
        assert (item.id, item.name, item.status, item.progress, item.msg) == (
            "111",
            "Restored",
            "pending",
            0.25,
            "queued",
        )

    def test_restart_recovery_requeues_interrupted_download(self):
        # DL-4 fixed: a persisted 'downloading' row is recovered as pending/failed.
        from rwmod.database import queue_upsert

        queue_upsert("111", name="Interrupted", status="downloading", progress=0.4)
        q = DownloadQueue()
        q._load_from_db()
        assert [i.id for i in q.items] == ["111"]
        assert q.items[0].status in ("pending", "failed")


class TestQueueAddAndNotify:
    def test_add_dedupes_pending_and_input(self):
        q = DownloadQueue()
        assert len(q.add(["1", "1"])) == 1
        assert q.add(["1"]) == []
        assert [i.id for i in q.items] == ["1"]

    def test_add_after_done_creates_new_item(self):
        q = DownloadQueue()
        q.add(["1"])
        q.items[0].status = "done"
        added = q.add(["1"])
        assert len(added) == 1 and len(q.items) == 2

    def test_add_before_start_does_not_crash(self):
        q = DownloadQueue()
        assert len(q.add(["1"])) == 1

    def test_snapshot_returns_independent_dicts(self):
        q = DownloadQueue()
        item = QueueItem(id="1", name="N", progress=0.5)
        q.items.append(item)
        snap = q.snapshot()
        assert snap == [
            {
                "id": "1",
                "name": "N",
                "instance_id": "",  # Wave 6b: snapshot carries the owner
                "status": "pending",
                "progress": 0.5,
                "msg": "",
                "downloaded": 0.0,
                "total": 0.0,
                "speed_bps": 0.0,
                "error_kind": "",
                "error_detail": "",
            }
        ]
        snap[0]["name"] = "mutated"
        assert item.name == "N"

    def test_notify_survives_bad_callback(self):
        q = DownloadQueue()
        q.items.append(QueueItem(id="1"))
        seen: list[list[dict]] = []

        async def bad(snapshot: list[dict]) -> None:
            raise RuntimeError("bad callback")

        async def good(snapshot: list[dict]) -> None:
            seen.append(snapshot)

        q.on_update(bad)
        q.on_update(good)
        asyncio.run(q._notify())
        assert len(seen) == 1 and seen[0][0]["id"] == "1"

    def test_readded_after_cancel_downloads_normally(self, tmp_path: Path, monkeypatch):
        # DL-5 fixed: a pending removal leaves no marker in _cancelled.
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        q.add(["111"])
        assert q.remove("111") is True  # pending item popped, nothing in flight
        added = q.add(["111"])  # user re-adds the same mod
        assert len(added) == 1
        monkeypatch.setattr(
            "rwmod.queue.download_batch",
            lambda config, ids, force=False, extra_out=None, progress_cb=None: {"111": True},
        )
        asyncio.run(q._process_batch(cfg, added, force=False))
        assert added[0].status == "done"


# ── autoupdate ─────────────────────────────────────────────────────


class TestAutoUpdateManager:
    def _run_cfg(self, tmp_path: Path) -> Config:
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path / "Mods")
        cfg.mods_dir.mkdir(exist_ok=True)
        return cfg

    def test_run_check_queues_outdated_with_force(self, tmp_path: Path, monkeypatch):
        cfg = self._run_cfg(tmp_path)
        updates = [{"workshop_id": "111", "name": "A"}, {"workshop_id": "222", "name": "B"}]
        monkeypatch.setattr("rwmod.autoupdate.Config.load", lambda: cfg)
        monkeypatch.setattr("rwmod.workshop.check_mod_updates", lambda mods_dir: updates)
        queue = MagicMock()
        queue.start = AsyncMock()
        # run_check() reports len(add(...)) -- the real queue returns the items
        # it actually created, so the mock must too.
        queue.add.return_value = [QueueItem(id="111"), QueueItem(id="222")]
        monkeypatch.setattr("rwmod.queue.get_queue", lambda: queue)

        m = AutoUpdateManager()
        seen: list[list[dict]] = []
        m.on_update(seen.append)
        result = asyncio.run(m.run_check())

        assert result == {"checked": 2, "outdated": 2, "queued": 2}
        # Wave 6b: the enqueued items belong to the active instance.
        queue.add.assert_called_once_with(["111", "222"], instance_id=cfg.active_instance)
        assert queue.start.await_args.kwargs["force"] is True
        assert seen == [updates]
        assert m.is_running is False

    def test_run_check_tags_items_with_the_active_instance(self, tmp_path: Path, monkeypatch):
        exe = tmp_path / "steamcmd.exe"
        alpha_mods = tmp_path / "AlphaMods"
        beta_mods = tmp_path / "BetaMods"
        alpha_mods.mkdir()
        beta_mods.mkdir()
        cfg = Config(
            instances=[
                InstanceConfig(id="alpha", steamcmd_path=exe, mods_dir=alpha_mods),
                InstanceConfig(id="beta", steamcmd_path=exe, mods_dir=beta_mods),
            ],
            active_instance="beta",
        )
        monkeypatch.setattr("rwmod.autoupdate.Config.load", lambda: cfg)
        monkeypatch.setattr(
            "rwmod.workshop.check_mod_updates", lambda mods_dir: [{"workshop_id": "111"}]
        )
        queue = MagicMock()
        queue.start = AsyncMock()
        queue.add.return_value = [QueueItem(id="111", instance_id="beta")]
        monkeypatch.setattr("rwmod.queue.get_queue", lambda: queue)

        result = asyncio.run(AutoUpdateManager().run_check())

        assert result["queued"] == 1
        queue.add.assert_called_once_with(["111"], instance_id="beta")

    def test_run_check_reentrancy_guard(self, tmp_path: Path, monkeypatch):
        cfg = self._run_cfg(tmp_path)
        monkeypatch.setattr("rwmod.autoupdate.Config.load", lambda: cfg)
        entered = threading.Event()
        release = threading.Event()

        def slow_check(mods_dir: str) -> list[dict]:
            entered.set()
            release.wait(5)
            return []

        monkeypatch.setattr("rwmod.workshop.check_mod_updates", slow_check)
        m = AutoUpdateManager()

        async def scenario() -> tuple[dict, dict]:
            first = asyncio.create_task(m.run_check())
            assert await asyncio.to_thread(entered.wait, 3)
            second = await m.run_check()
            release.set()
            return await first, second

        first_result, second_result = asyncio.run(asyncio.wait_for(scenario(), timeout=15))
        assert first_result == {"checked": 0, "outdated": 0, "queued": 0}
        assert second_result == {"ok": False, "msg": "已在运行中"}

    def test_last_result_is_a_copy_and_clearable(self):
        m = AutoUpdateManager()
        m._check_result.extend([{"a": 1}])
        snapshot = m.last_result
        snapshot.append({"b": 2})
        assert m.last_result == [{"a": 1}]
        m.clear_result()
        assert m.last_result == []

    def test_notify_isolates_bad_callback(self):
        m = AutoUpdateManager()
        seen: list[list[dict]] = []

        def bad(updates: list[dict]) -> None:
            raise RuntimeError("bad callback")

        m.on_update(bad)
        m.on_update(seen.append)
        m._notify([{"x": 1}])
        assert seen == [[{"x": 1}]]

    def test_background_start_stop_idempotent(self):
        m = AutoUpdateManager()

        async def scenario() -> asyncio.Task:
            await m.start_background()
            task = m._bg_task
            assert task is not None
            await m.start_background()
            assert m._bg_task is task
            await m.stop_background()
            return task

        task = asyncio.run(scenario())
        assert task.done()

    def test_background_loop_records_updates(self, tmp_path: Path, monkeypatch):
        updates = [{"workshop_id": "111", "name": "A"}]
        monkeypatch.setattr("rwmod.workshop.check_mod_updates", lambda mods_dir: updates)
        calls = {"n": 0}

        async def fake_sleep(seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > 1:  # let the body run exactly once
                raise _StopLoop

        monkeypatch.setattr(
            "rwmod.autoupdate.asyncio",
            SimpleNamespace(sleep=fake_sleep, to_thread=asyncio.to_thread),
        )
        m = AutoUpdateManager()
        seen: list[list[dict]] = []
        m.on_update(seen.append)
        with pytest.raises(_StopLoop):
            asyncio.run(m._loop())
        assert calls["n"] == 2
        assert m.last_result == updates
        assert seen == [updates]

    def test_background_loop_survives_check_failure(self, monkeypatch):
        def boom(mods_dir: str) -> list[dict]:
            raise RuntimeError("network down")

        monkeypatch.setattr("rwmod.workshop.check_mod_updates", boom)
        calls = {"n": 0}

        async def fake_sleep(seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise _StopLoop

        monkeypatch.setattr(
            "rwmod.autoupdate.asyncio",
            SimpleNamespace(sleep=fake_sleep, to_thread=asyncio.to_thread),
        )
        m = AutoUpdateManager()
        with pytest.raises(_StopLoop):
            asyncio.run(m._loop())
        assert calls["n"] == 2  # the failure was logged and the loop continued
        assert m.last_result == []


# ── offline ────────────────────────────────────────────────────────


class TestOfflineState:
    def test_fresh_state_is_optimistic(self):
        assert offline_mod.get_status() == {
            "online": True,
            "last_check": 0,
            "last_check_ago_sec": 0,
        }

    def test_mark_offline_records_epoch_timestamp(self):
        offline_mod.mark_offline()
        status = offline_mod.get_status()
        assert status["online"] is False
        assert abs(status["last_check"] - time.time()) < 5  # epoch, not monotonic
        assert status["last_check_ago_sec"] >= 0

    def test_mark_online_after_offline(self):
        offline_mod.mark_offline()
        offline_mod.mark_online()
        assert offline_mod.get_status()["online"] is True

    def test_request_json_network_failure_marks_offline(self, monkeypatch):
        opener = SimpleNamespace(open=MagicMock(side_effect=OSError("net down")))
        monkeypatch.setattr("rwmod.workshop._shared_opener", opener)
        assert workshop_mod._request_json("https://api.example.invalid/x") is None
        assert offline_mod.get_status()["online"] is False

    def test_request_json_success_marks_online(self, monkeypatch):
        payload = json.dumps({"ok": 1}).encode()
        monkeypatch.setattr(
            "rwmod.workshop._shared_opener",
            SimpleNamespace(open=lambda req, timeout=15: _FakeResp(payload)),
        )
        assert workshop_mod._request_json("https://api.example.invalid/x") == {"ok": 1}
        assert offline_mod.get_status()["online"] is True

    def test_request_json_non_dict_payload_is_none_but_online(self, monkeypatch):
        monkeypatch.setattr(
            "rwmod.workshop._shared_opener",
            SimpleNamespace(open=lambda req, timeout=15: _FakeResp(b"[1, 2]")),
        )
        assert workshop_mod._request_json("https://api.example.invalid/x") is None
        assert offline_mod.get_status()["online"] is True


# ── workshop helpers feeding the download chain ────────────────────


class TestWorkshopHelpers:
    def test_get_collection_details_requires_result_1(self, monkeypatch):
        monkeypatch.setattr(
            "rwmod.workshop._request_json", lambda *a, **k: {"response": {"result": 9}}
        )
        assert workshop_mod._get_collection_details("999") is None
        monkeypatch.setattr(
            "rwmod.workshop._request_json", lambda *a, **k: {"response": {"result": 1, "x": 2}}
        )
        assert workshop_mod._get_collection_details("999") == {"result": 1, "x": 2}
        monkeypatch.setattr("rwmod.workshop._request_json", lambda *a, **k: {"response": "weird"})
        assert workshop_mod._get_collection_details("999") is None
        monkeypatch.setattr("rwmod.workshop._request_json", lambda *a, **k: None)
        assert workshop_mod._get_collection_details("999") is None

    def test_fetch_batch_parses_details(self, monkeypatch):
        payload = {
            "response": {
                "publishedfiledetails": [
                    {"publishedfileid": "111", "time_updated": 42, "title": "T"},
                    {"publishedfileid": "222", "time_updated": 0},
                ]
            }
        }
        monkeypatch.setattr("rwmod.workshop._request_json", lambda *a, **k: payload)
        details = workshop_mod._fetch_batch(["111", "222"])
        assert details["111"]["time_updated"] == 42
        assert details["111"]["title"] == "T"
        assert details["222"]["time_updated"] == 0

    def test_fetch_item_dependencies_returns_empty_on_network_failure(self, monkeypatch):
        monkeypatch.setattr("rwmod.workshop._request_json", lambda *a, **k: None)
        assert workshop_mod.fetch_item_dependencies(["111"]) == {}

    def test_scrape_collection_page_excludes_self_and_dedupes(self, monkeypatch):
        html = (
            '<a id="sharedfile_999" href="#">self</a>'
            '<a id="sharedfile_111" href="#">a</a>'
            '<a id="sharedfile_111" href="#">dup</a>'
            '<a id="sharedfile_222" href="#">b</a>'
        )
        monkeypatch.setattr(
            "rwmod.workshop._shared_opener",
            SimpleNamespace(open=lambda req, timeout=30: _FakeResp(html.encode())),
        )
        assert workshop_mod._scrape_collection_page("999") == ["111", "222"]

    def test_search_workshop_parses_and_drops_youtube_preview(self, monkeypatch):
        payload = {
            "response": {
                "publishedfiledetails": [
                    {
                        "publishedfileid": "111",
                        "title": "T",
                        "creator": "C",
                        "preview_url": "https://youtu.be/xyz",
                        "subscriptions": 5,
                    }
                ]
            }
        }
        monkeypatch.setattr("rwmod.workshop._request_json", lambda *a, **k: payload)
        results = workshop_mod.search_workshop("q")
        assert len(results) == 1
        assert results[0].preview_url == ""
        monkeypatch.setattr("rwmod.workshop._request_json", lambda *a, **k: None)
        assert workshop_mod.search_workshop("q") == []


# ── skymods SSRF guard (fallback source for the download chain) ────


class TestSkymodsUrlGuard:
    def test_rejects_non_http_scheme(self):
        from rwmod.skymods import _is_safe_download_url

        assert _is_safe_download_url("file:///etc/passwd") is False

    def test_rejects_unlisted_host(self):
        from rwmod.skymods import _is_safe_download_url

        assert _is_safe_download_url("https://evil.example.com/mod.zip") is False

    def test_rejects_private_address_for_allowed_host(self, monkeypatch):
        from rwmod import skymods

        monkeypatch.setattr(skymods.socket, "gethostbyname", lambda host: "127.0.0.1")
        assert skymods._is_safe_download_url("https://smods.ru/mod.zip") is False
        monkeypatch.setattr(skymods.socket, "gethostbyname", lambda host: "169.254.169.254")
        assert skymods._is_safe_download_url("https://modsbase.com/mod.zip") is False

    def test_accepts_public_allowed_host(self, monkeypatch):
        from rwmod import skymods

        monkeypatch.setattr(skymods.socket, "gethostbyname", lambda host: "93.184.216.34")
        assert skymods._is_safe_download_url("https://smods.ru/mod.zip") is True
        # Subdomains of an allowed domain are covered too.
        assert skymods._is_safe_download_url("https://cdn.modsbase.com/mod.zip") is True

    def test_rejects_unresolvable_host(self, monkeypatch):
        from rwmod import skymods

        def boom(host: str) -> str:
            raise OSError("no dns")

        monkeypatch.setattr(skymods.socket, "gethostbyname", boom)
        assert skymods._is_safe_download_url("https://smods.ru/mod.zip") is False


# ── confirmed defect probes (xfail strict) ─────────────────────────


class TestKnownDefects:
    @pytest.mark.skipif(os.name != "nt", reason="Windows process-group semantics only")
    def test_spawn_creates_new_process_group_for_ctrl_break(self, sc: SteamCMD):
        proc = MagicMock()
        proc.wait.return_value = 0
        proc.stdout = io.StringIO("")
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc) as mock_popen:
            sc.workshop_download_many(["111"])
        flags = mock_popen.call_args.kwargs.get("creationflags", 0)
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP

    def test_batch_force_install_failure_preserves_old_mod(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        old = _install_mod(cfg.mods_dir, "author.mod", "111", pkg="author.mod")
        _workshop_content(cfg, "111", "author.mod")
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        monkeypatch.setattr(
            "rwmod.downloader.SteamCMD.workshop_download_many",
            lambda self, ids, progress_cb=None, instance_id=None: {m: _res(True, m) for m in ids},
        )

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("rwmod.downloader.shutil.copytree", boom)
        monkeypatch.setattr("rwmod.downloader._skymods_fallback", lambda config, mid: False)
        result = download_batch(cfg, ["111"], force=True)
        assert result == {"111": False}
        assert old.is_dir(), "failed force-overwrite must keep the old mod"

    def test_installed_collection_reports_success(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _install_mod(cfg.mods_dir, "ChildOne", "100", pkg="a.one")
        _install_mod(cfg.mods_dir, "ChildTwo", "200", pkg="a.two")
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: mid == "999")
        monkeypatch.setattr("rwmod.workshop.fetch_collection_children", lambda mid: ["100", "200"])
        assert download_one(cfg, "999") is True


# ── additional edge coverage ───────────────────────────────────────


class TestSteamCmdCancelEdges:
    def test_cancel_returns_false_when_signal_delivery_fails(self):
        proc = MagicMock()
        proc.send_signal.side_effect = OSError("cannot signal")
        proc.terminate.side_effect = OSError("cannot signal")
        _live_procs.clear()
        _live_procs[("", "777")] = proc
        assert cancel_download(None, "777") is False
        assert ("", "777") not in _live_procs


class TestProgressReaderEdges:
    def test_reader_without_stdout_returns_immediately(self):
        from rwmod.steamcmd import _ProgressReader

        reader = _ProgressReader(None, {"111"}, None)
        reader.start()
        reader.join(2)
        assert reader.lines == [] and reader.current_id is None

    def test_reader_stops_after_item_end_and_skips_blank_lines(self):
        from rwmod.steamcmd import _ProgressReader

        out = io.StringIO(
            "\n"
            "Downloading item 111 ...\n"
            "Update state (0x61) downloading, progress: 10.00 (1 / 10)\n"
            "Success. Downloaded item 111\n"
            "Update state (0x61) downloading, progress: 90.00 (9 / 10)\n"
            "Downloading item 222 ...\n"
        )
        seen: list[tuple] = []
        reader = _ProgressReader(out, {"111", "222"}, lambda *a: seen.append(a))
        reader.start()
        reader.join(5)
        assert [s[1] for s in seen] == [10.0]  # progress after item end is ignored
        assert len(reader.lines) == 5  # the blank line was dropped


class TestDownloadOneInstallFallback:
    def test_install_failure_falls_back_to_skymods(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")
        fake = _patch_steam(monkeypatch, cfg, [_res(True, "111")], skymods=True)

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("rwmod.downloader.shutil.copytree", boom)
        assert download_one(cfg, "111") is True
        assert fake.calls == ["111"]

    def test_download_one_does_not_retry_cancelled(self, tmp_path: Path, monkeypatch):
        """P1-1: CANCELLED is terminal for this call — no retry, no Skymods."""
        cfg = _cfg(tmp_path)
        fake = _patch_steam(
            monkeypatch, cfg, [_res(False, "111", ErrorKind.CANCELLED, "SteamCMD 批次被取消")]
        )
        outcome: list = []
        assert download_one(cfg, "111", outcome_out=outcome) is False
        assert fake.calls == ["111"]  # one attempt only
        assert outcome[0].error_kind == ErrorKind.CANCELLED

    def test_retry_logs_matching_output_lines(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.mod")
        first = DownloadResult(
            False,
            "111",
            ErrorKind.UNKNOWN,
            "blip",
            output_lines=["Downloading item 111", "ERROR: transient failure"],
        )
        fake = _patch_steam(monkeypatch, cfg, [first, _res(True, "111")])
        assert download_one(cfg, "111") is True
        assert len(fake.calls) == 2


class TestQueueResilience:
    def test_persist_failures_do_not_break_queue_ops(self, monkeypatch):
        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr("rwmod.database.queue_upsert", boom)
        monkeypatch.setattr("rwmod.database.queue_delete", boom)
        monkeypatch.setattr("rwmod.database.queue_clear_done", boom)
        q = DownloadQueue()
        assert len(q.add(["1"])) == 1
        assert q.remove("1") is True
        q.add(["2"])
        q.items[0].status = "done"
        q.clear_done()
        assert q.items == []

    def test_load_from_db_failure_starts_empty(self, monkeypatch):
        def boom() -> list[dict]:
            raise RuntimeError("db down")

        monkeypatch.setattr("rwmod.database.queue_load_pending", boom)
        q = DownloadQueue()
        q._load_from_db()
        assert q.items == []

    def test_progress_for_unknown_id_is_ignored(self, tmp_path: Path, monkeypatch):
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        item = QueueItem(id="111")
        q.items.append(item)

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            progress_cb("999", 50.0, 5.0, 10.0)  # not part of this batch
            return {"111": True}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)
        asyncio.run(q._process_batch(cfg, [item], force=False))
        assert item.status == "done" and item.progress == 1.0

    def test_get_queue_returns_singleton(self):
        import rwmod.queue as qmod
        from rwmod.queue import get_queue

        qmod._queue = None
        try:
            first = get_queue()
            assert get_queue() is first
        finally:
            qmod._queue = None


# ── Wave 6b: downloads are scoped to the queue item's instance ─────


class TestInstanceDownloadIsolation:
    @staticmethod
    def _two_instance_cfg(tmp_path: Path) -> tuple[Config, Path, Path]:
        exe = tmp_path / "steamcmd" / "steamcmd.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.touch()
        alpha_mods = tmp_path / "AlphaMods"
        beta_mods = tmp_path / "BetaMods"
        alpha_mods.mkdir()
        beta_mods.mkdir()
        cfg = Config(
            instances=[
                InstanceConfig(
                    id="alpha",
                    steamcmd_path=exe,
                    mods_dir=alpha_mods,
                    rimworld_dir=tmp_path / "AlphaRW",
                    backup_dir=tmp_path / "AlphaBak",
                ),
                InstanceConfig(
                    id="beta",
                    steamcmd_path=exe,
                    mods_dir=beta_mods,
                    rimworld_dir=tmp_path / "BetaRW",
                    backup_dir=tmp_path / "BetaBak",
                ),
            ],
            active_instance="beta",
        )
        return cfg, alpha_mods, beta_mods

    def test_find_existing_is_scoped_to_the_requested_instance(self, tmp_path: Path, monkeypatch):
        """Two instances may hold a same-named folder for different mods.

        The process-global active instance is ``alpha``; a lookup for the
        *beta* item must not see alpha's metadata row (the old unscoped query
        returned alpha's ``Shared`` folder and, because beta happens to have a
        folder with the same name, reported mod 111 as already installed).
        """
        from rwmod.database import upsert_local_mod_workshop_id

        _cfg, alpha_mods, beta_mods = self._two_instance_cfg(tmp_path)
        _install_mod(alpha_mods, "Shared", "111", pkg="alpha.one")
        _install_mod(beta_mods, "Shared", "222", pkg="beta.two")
        upsert_local_mod_workshop_id("Shared", "111", instance_id="alpha")
        upsert_local_mod_workshop_id("Shared", "222", instance_id="beta")

        monkeypatch.setattr("rwmod.config.current_instance_id", lambda: "alpha")

        assert _find_existing(beta_mods, "111", instance_id="beta") is None
        assert _find_existing(alpha_mods, "111", instance_id="alpha") == alpha_mods / "Shared"

    def test_batch_downloads_a_mod_the_other_instance_already_has(
        self, tmp_path: Path, monkeypatch
    ):
        """beta's queue item for 111 must download even though alpha has 111."""
        from rwmod.database import upsert_local_mod_workshop_id

        cfg, alpha_mods, beta_mods = self._two_instance_cfg(tmp_path)
        _install_mod(alpha_mods, "Shared", "111", pkg="alpha.one")
        _install_mod(beta_mods, "Shared", "222", pkg="beta.two")
        upsert_local_mod_workshop_id("Shared", "111", instance_id="alpha")
        upsert_local_mod_workshop_id("Shared", "222", instance_id="beta")

        _workshop_content(cfg, "111", "beta.one")
        monkeypatch.setattr("rwmod.config.current_instance_id", lambda: "alpha")
        monkeypatch.setattr("rwmod.workshop.is_collection", lambda mid: False)
        calls: list[list[str]] = []

        def fake_many(self, ids, progress_cb=None, instance_id=None):
            calls.append(list(ids))
            return {mid: _res(True, mid) for mid in ids}

        monkeypatch.setattr("rwmod.downloader.SteamCMD.workshop_download_many", fake_many)

        assert download_batch(cfg, ["111"]) == {"111": True}
        assert calls == [["111"]]
        # Installed into beta only; alpha's copy was neither used nor touched.
        assert (beta_mods / "beta.one").is_dir()
        assert not (alpha_mods / "beta.one").exists()
        assert (alpha_mods / "Shared" / "About" / "About.xml").exists()
