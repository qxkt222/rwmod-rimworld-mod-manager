"""BE-B wave 1 — download kernel / sorting hardening regressions.

Covers the ten items of the BE-B task book:

* stash failure without any backup must never delete the old mod;
* installs are locked by destination folder (duplicate packageIds);
* cancellation works before the SteamCMD process exists;
* a failed Steam API check is an explicit error, never "no updates";
* ModsConfig writes use a unique temp file per writer;
* an explicit community-rules refresh bypasses the 24 h cache TTL;
* database downloads are read with a hard size cap;
* ``compare_modsconfig`` exempts official DLC.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from rwmod.config import Config
from rwmod.errors import WorkshopError

# ── helpers ────────────────────────────────────────────────────────


def _cfg(tmp_path: Path) -> Config:
    exe = tmp_path / "steamcmd" / "steamcmd.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.touch()
    cfg = Config(steamcmd_path=exe, mods_dir=tmp_path / "Mods")
    cfg.mods_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir = tmp_path / "Backups"
    return cfg


def _workshop_content(cfg: Config, wid: str, pkg: str) -> Path:
    d = cfg.steamcmd_path.parent / "steamapps" / "workshop" / "content" / "294100" / wid
    (d / "About").mkdir(parents=True, exist_ok=True)
    (d / "About" / "About.xml").write_text(
        f"<ModMetaData><name>Downloaded</name><packageId>{pkg}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    return d


def _install_mod(mods_dir: Path, folder: str, wid: str, pkg: str | None = None) -> Path:
    d = mods_dir / folder
    (d / "About").mkdir(parents=True, exist_ok=True)
    (d / "About" / "About.xml").write_text(
        f"<ModMetaData><name>{folder}</name><packageId>{pkg or folder}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    (d / "About" / "PublishedFileId.txt").write_text(wid, encoding="utf-8")
    return d


class _ChunkedResponse:
    """HTTP response double that honours ``read(size)`` and records the bytes served."""

    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self._payload = payload
        self._pos = 0
        self.status = status
        self.headers = {"ETag": '"x"'}
        self.served = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._payload) - self._pos
        chunk = self._payload[self._pos : self._pos + n]
        self._pos += len(chunk)
        self.served += len(chunk)
        return chunk

    def __enter__(self) -> _ChunkedResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


# ── A1: stash failure must not delete the old mod ──────────────────


class TestStashFailureKeepsOldCopy:
    @staticmethod
    def _broken_rename(self: Path, target: object) -> None:
        raise OSError("simulated rename failure")

    def test_batch_install_keeps_old_mod_without_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod.downloader import _install_content

        cfg = _cfg(tmp_path)
        cfg.update_backup = False  # no persistent zip is taken
        old = _install_mod(cfg.mods_dir, "author.mod", "999", "author.mod")
        marker = old / "About" / "marker.txt"
        marker.write_text("old", encoding="utf-8")
        _workshop_content(cfg, "111", "author.mod")

        monkeypatch.setattr(Path, "rename", self._broken_rename)

        assert _install_content(cfg, "111", force=True) is False
        # The only copy of the installed mod must still be there.
        assert (old / "About" / "About.xml").is_file()
        assert marker.read_text(encoding="utf-8") == "old"

    def test_single_download_keeps_old_mod_without_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import patch

        from rwmod.downloader import download_one

        cfg = _cfg(tmp_path)
        cfg.update_backup = False
        old = _install_mod(cfg.mods_dir, "MyOldMod", "123456", "author.old")

        monkeypatch.setattr(Path, "rename", self._broken_rename)
        with patch("rwmod.workshop.is_collection", return_value=False):
            assert download_one(cfg, "123456", force=True) is False

        assert (old / "About" / "About.xml").is_file()


# ── A3: installs are serialized by destination folder ──────────────


class TestDestinationFolderLock:
    def test_two_ids_with_the_same_package_id_do_not_clobber(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod.downloader import _install_content

        cfg = _cfg(tmp_path)
        _workshop_content(cfg, "111", "author.same")
        _workshop_content(cfg, "222", "author.same")

        import rwmod.downloader as dl_mod

        real_copytree = dl_mod.shutil.copytree

        def slow_copytree(src: object, dst: object, *args: object, **kwargs: object) -> object:
            # Widen the window in which two unlocked installs would overlap.
            time.sleep(0.15)
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr("rwmod.downloader.shutil.copytree", slow_copytree)

        results: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def run(mid: str) -> None:
            barrier.wait(5)
            results[mid] = _install_content(cfg, mid, force=True)

        threads = [threading.Thread(target=run, args=(mid,)) for mid in ("111", "222")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert results == {"111": True, "222": True}
        dest = cfg.mods_dir / "author.same"
        assert (dest / "About" / "About.xml").is_file()
        assert not list(cfg.mods_dir.glob(".rwmod_stash_*"))


# ── A2: cancellation before the process exists ─────────────────────


@pytest.fixture
def steamcmd_state():
    from rwmod.steamcmd_support import procs

    def _clean() -> None:
        with procs._live_guard:
            procs._live_procs.clear()
            procs._cancelled_procs.clear()
            procs._cancel_requests.clear()

    _clean()
    yield procs
    _clean()


class TestCancelBeforeSpawn:
    def test_cancel_requested_before_call_never_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steamcmd_state
    ):
        from rwmod import steamcmd as sc

        exe = tmp_path / "steamcmd.exe"
        exe.touch()
        popen_calls: list[tuple] = []
        monkeypatch.setattr(
            "rwmod.steamcmd.subprocess.Popen",
            lambda *a, **k: popen_calls.append((a, k)),
        )

        assert sc.request_cancel(None, "111") is False  # nothing live yet
        results = sc.SteamCMD(exe).workshop_download_many(["111"])

        assert popen_calls == []
        assert results["111"].error_kind == sc.ErrorKind.CANCELLED
        assert results["111"].success is False
        # The pending marker is consumed — a later download of the same id runs.
        assert ("", "111") not in steamcmd_state._cancel_requests

    def test_cancel_while_waiting_for_the_global_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steamcmd_state
    ):
        from rwmod import steamcmd as sc

        exe = tmp_path / "steamcmd.exe"
        exe.touch()
        sc_cmd = sc.SteamCMD(exe)
        popen_calls: list[tuple] = []
        monkeypatch.setattr(
            "rwmod.steamcmd.subprocess.Popen",
            lambda *a, **k: popen_calls.append((a, k)),
        )

        lock = steamcmd_state._run_lock
        lock.acquire()
        results: dict[str, object] = {}
        worker = threading.Thread(
            target=lambda: results.update(sc_cmd.workshop_download_many(["111"]))
        )
        try:
            worker.start()
            time.sleep(0.3)  # worker is now blocked on the run lock
            sc.request_cancel(None, "111")
            worker.join(5)
        finally:
            lock.release()

        assert not worker.is_alive()
        assert popen_calls == []
        assert results["111"].error_kind == sc.ErrorKind.CANCELLED


# ── A4: Steam API failure is never "no updates" ────────────────────


class _OfflineOpener:
    def open(self, *_args: object, **_kwargs: object) -> None:
        raise OSError("offline")


class TestUpdateCheckFailure:
    def test_check_mod_updates_raises_on_api_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import patch

        from rwmod import workshop
        from rwmod.database import close_db, init_db

        mod = tmp_path / "Mods" / "SomeMod"
        (mod / "About").mkdir(parents=True)
        (mod / "About" / "About.xml").write_text(
            "<ModMetaData><name>Some</name><packageId>a.some</packageId></ModMetaData>",
            encoding="utf-8",
        )
        (mod / "About" / "PublishedFileId.txt").write_text("100", encoding="utf-8")

        monkeypatch.setattr(workshop, "_shared_opener", _OfflineOpener())

        db_path = tmp_path / "updates.db"
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            try:
                with pytest.raises(WorkshopError):
                    workshop.check_mod_updates(str(tmp_path / "Mods"))
            finally:
                close_db()

    def _make_mods(self, mods_dir: Path, count: int) -> None:
        for i in range(count):
            mod = mods_dir / f"Mod{i}"
            (mod / "About").mkdir(parents=True)
            (mod / "About" / "About.xml").write_text(
                f"<ModMetaData><name>Mod{i}</name><packageId>a.mod{i}</packageId></ModMetaData>",
                encoding="utf-8",
            )
            (mod / "About" / "PublishedFileId.txt").write_text(str(1000 + i), encoding="utf-8")

    def test_one_failed_batch_is_partial_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """One flaky batch must not throw away the other batches' answer."""
        from unittest.mock import patch

        from rwmod import workshop
        from rwmod.database import close_db, init_db

        mods_dir = tmp_path / "Mods"
        self._make_mods(mods_dir, 101)  # 2 batches at the default batch_size

        def fake_fetch_batch(ids: list[str]) -> dict[str, dict] | None:
            # Fail exactly the batch that owns id 1050; every other batch answers.
            if "1050" in ids:
                return None
            return {wid: {"time_updated": 1, "title": wid} for wid in ids}

        monkeypatch.setattr(workshop, "_fetch_batch", fake_fetch_batch)

        db_path = tmp_path / "updates.db"
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            try:
                result = workshop.check_mod_updates(str(mods_dir))
            finally:
                close_db()

        assert result["partial"] is True
        assert result["failed_batches"] == 1
        assert result["total_batches"] == 2
        # The surviving batch only answered with timestamps, and first-time
        # checks seed instead of reporting; nothing is falsely "updated".
        assert result["updates"] == []

    def test_all_batches_succeed_reports_complete_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import patch

        from rwmod import workshop
        from rwmod.database import close_db, init_db

        mods_dir = tmp_path / "Mods"
        self._make_mods(mods_dir, 101)

        def fake_fetch_batch(ids: list[str]) -> dict[str, dict]:
            return {wid: {"time_updated": 1, "title": wid} for wid in ids}

        monkeypatch.setattr(workshop, "_fetch_batch", fake_fetch_batch)

        db_path = tmp_path / "updates.db"
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            try:
                result = workshop.check_mod_updates(str(mods_dir))
            finally:
                close_db()

        assert result == {
            "updates": [],
            "partial": False,
            "failed_batches": 0,
            "total_batches": 2,
        }

    def test_autoupdate_surfaces_failure_instead_of_all_latest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod.autoupdate import AutoUpdateManager

        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.autoupdate.Config.load", lambda: cfg)

        def boom(_mods_dir: str) -> list[dict]:
            raise WorkshopError("Steam API 请求失败")

        monkeypatch.setattr("rwmod.workshop.check_mod_updates", boom)
        manager = AutoUpdateManager()

        with pytest.raises(WorkshopError):
            asyncio.run(manager.run_check())
        assert "Steam API" in manager.last_error

    def test_autoupdate_clears_last_error_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod.autoupdate import AutoUpdateManager

        cfg = _cfg(tmp_path)
        monkeypatch.setattr("rwmod.autoupdate.Config.load", lambda: cfg)
        monkeypatch.setattr(
            "rwmod.workshop.check_mod_updates",
            lambda _d: {
                "updates": [],
                "partial": False,
                "failed_batches": 0,
                "total_batches": 0,
            },
        )

        manager = AutoUpdateManager()
        manager._last_error = "stale failure"
        assert asyncio.run(manager.run_check()) == {
            "checked": 0,
            "outdated": 0,
            "queued": 0,
        }
        assert manager.last_error == ""


# ── B5: unique ModsConfig temp file per writer ─────────────────────


class TestModsconfigTempUniqueness:
    def test_concurrent_writers_never_share_a_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.undo as undo_mod
        from rwmod.modsconfig_edit import write_modsconfig_target

        root = tmp_path / "sandbox"
        (root / "Config").mkdir(parents=True)
        monkeypatch.setenv("RWMOD_TEST_SANDBOX_HOME", str(root))
        monkeypatch.setattr(undo_mod, "UNDO_DIR", root / "undo")

        target = root / "Config" / "ModsConfig.xml"
        target.write_text("<ModsConfigData/>", encoding="utf-8")

        import rwmod.modsconfig_edit as mce

        seen_tmp: list[str] = []
        real_replace = mce.os.replace
        lock = threading.Lock()

        def spy_replace(src: object, dst: object) -> None:
            name = Path(src).name
            if name.endswith(".rwmod.tmp"):
                with lock:
                    seen_tmp.append(name)
            real_replace(src, dst)

        monkeypatch.setattr("rwmod.modsconfig_edit.os.replace", spy_replace)

        texts = [f"<ModsConfigData><v>{i}</v></ModsConfigData>" for i in range(6)]
        barrier = threading.Barrier(len(texts))
        errors: list[Exception] = []

        def write(text: str) -> None:
            barrier.wait(5)
            try:
                write_modsconfig_target(target, text)
            except Exception as e:  # noqa: BLE001 — asserted below
                errors.append(e)

        threads = [threading.Thread(target=write, args=(t,)) for t in texts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert errors == []
        assert target.read_text(encoding="utf-8") in texts
        assert len(seen_tmp) == len(texts)
        assert len(set(seen_tmp)) == len(texts), "two writers shared one temp file"
        assert not list(target.parent.glob("*.rwmod.tmp"))


# ── B6: explicit community-rules refresh really downloads ──────────


class TestCommunityRulesRefresh:
    def _fresh_cache(self, cache_dir: Path) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "community_rules.json"
        path.write_text(
            json.dumps({"rules": {"a.mod": {"loadTop": {"value": True}}}}),
            encoding="utf-8",
        )
        return path

    def test_refresh_bypasses_a_fresh_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db

        cache_dir = tmp_path / "cache"
        self._fresh_cache(cache_dir)  # mtime = now → TTL would say "fresh"
        payload = json.dumps({"rules": {"b.mod": {"loadBottom": {"value": True}}}}).encode()
        calls: list[object] = []
        monkeypatch.setattr(
            rimpy_db,
            "urlopen",
            lambda req, timeout=30: calls.append(req) or _ChunkedResponse(payload),
        )

        rimpy_db.RimPyDB.reset()
        rimpy_db.RimPyDB.get(cache_dir)

        assert rimpy_db.refresh_community_rules() is True
        assert len(calls) == 1, "refresh was a no-op because the cache was fresh"
        assert "b.mod" in (cache_dir / "community_rules.json").read_text(encoding="utf-8")

    def test_refresh_failure_reports_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db

        cache_dir = tmp_path / "cache"
        self._fresh_cache(cache_dir)

        def boom(_req: object, timeout: int = 30) -> None:
            raise OSError("offline")

        monkeypatch.setattr(rimpy_db, "urlopen", boom)
        rimpy_db.RimPyDB.reset()
        db = rimpy_db.RimPyDB.get(cache_dir)

        assert rimpy_db.refresh_community_rules() is False
        assert db.last_refresh_error.startswith("下载失败")
        # Fail-open: the stale cache is still usable.
        assert db.rules["a.mod"]["loadTop"] is True


# ── B7: bounded / streaming database downloads ─────────────────────


class TestBoundedDownloads:
    def test_steam_db_refresh_stops_at_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        response = _ChunkedResponse(b"x" * 4096)
        monkeypatch.setattr(sd_mod, "urlopen", lambda req, timeout=30: response)
        db = sd_mod.SteamDB(cache_dir=tmp_path / "cache")

        assert db.refresh(max_bytes=32) is False
        assert response.served <= 33, "the whole payload was buffered before the cap check"

    def test_rimpy_db_download_stops_at_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db

        monkeypatch.setattr(rimpy_db, "MAX_CACHE_BYTES", 32)
        response = _ChunkedResponse(b"y" * 4096)
        monkeypatch.setattr(rimpy_db, "urlopen", lambda req, timeout=30: response)
        db = rimpy_db.RimPyDB(cache_dir=tmp_path / "cache")

        assert db.ensure_loaded() is False
        assert response.served <= 33, "the whole payload was buffered before the cap check"


# ── B8: compare exempts official DLC ───────────────────────────────


class TestCompareDlcExemption:
    def test_official_dlc_package_ids_are_not_missing(self, tmp_path: Path):
        from rwmod.rimsort import compare_modsconfig

        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        _install_mod(mods_dir, "A", "111", "a.mod")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods>"
            "<li>ludeon.rimworld</li>"
            "<li>ludeon.rimworld.royalty</li>"
            "<li>ludeon.rimworld.ideology</li>"
            "<li>ludeon.rimworld.biotech</li>"
            "<li>ludeon.rimworld.anomaly</li>"
            "<li>ludeon.rimworld.odyssey</li>"
            "<li>a.mod</li>"
            "</activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        data = compare_modsconfig(cfg, mods_dir)

        assert data["missing"] == []
        assert data["missing_count"] == 0
        assert data["extra"] == []
