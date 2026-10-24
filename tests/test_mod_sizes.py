"""Unit tests for mod_sizes.py — background disk-size snapshots.

The real Mods tree is ~150k files / ~20GB; walking it on every /api/mods and
/api/dashboard request took 5-8 seconds. These tests pin the replacement
contract: a reader always returns the last snapshot immediately, one daemon
scan at a time, TTL-based refresh and explicit invalidation.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

import rwmod.mod_sizes as mod_sizes


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    mod_sizes.stop_all()
    mod_sizes.invalidate()
    yield
    mod_sizes.stop_all()
    mod_sizes.invalidate()


def _make_mod(mods: Path, folder: str, files: int = 2, size: int = 1024) -> Path:
    """Create a minimal valid mod with nested files."""
    mod = mods / folder
    about = mod / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        "<ModMetaData><name>x</name><packageId>a.b</packageId></ModMetaData>",
        encoding="utf-8",
    )
    (about / "PublishedFileId.txt").write_text("1", encoding="utf-8")
    defs = mod / "Defs" / "Nested"
    defs.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (defs / f"f{i}.bin").write_bytes(b"x" * size)
    return mod


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            total += (Path(root) / name).stat().st_size
    return total


class TestSnapshotContract:
    def test_first_call_is_empty_then_scan_fills_it(self, tmp_path: Path) -> None:
        mods = tmp_path / "Mods"
        _make_mod(mods, "ModA", files=3, size=2048)
        _make_mod(mods, "ModB", files=1, size=512)

        sizes = mod_sizes.get_sizes(mods)
        # The reader must never block: the very first call returns the empty
        # pre-scan snapshot even though the worker may already be running.
        assert sizes == {}
        assert mod_sizes.wait_for_scan(mods, timeout=5.0) is True

        sizes = mod_sizes.get_sizes(mods)
        assert sizes["ModA"] == _tree_size(mods / "ModA")
        assert sizes["ModB"] == _tree_size(mods / "ModB")
        assert mod_sizes.size_mb(mods, "ModA") == round(sizes["ModA"] / (1024 * 1024), 2)
        assert mod_sizes.size_mb(mods, "missing") == 0.0

    def test_first_call_does_not_wait_for_a_slow_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mods = tmp_path / "Mods"
        mod = _make_mod(mods, "ModA")
        release = threading.Event()
        started = threading.Event()
        real_scan = mod_sizes._scan_dir

        def slow_scan(path: Path, exclude: frozenset[str]) -> dict[str, int]:
            started.set()
            assert release.wait(5.0), "test never released the scan"
            return real_scan(path, exclude)

        monkeypatch.setattr(mod_sizes, "_scan_dir", slow_scan)

        t0 = time.perf_counter()
        sizes = mod_sizes.get_sizes(mods)
        elapsed = time.perf_counter() - t0

        assert sizes == {}
        assert elapsed < 1.0, f"reader blocked for {elapsed:.2f}s"
        assert started.wait(2.0), "background scan never started"
        assert mod_sizes.is_pending(mods) is True

        release.set()
        assert mod_sizes.wait_for_scan(mods, timeout=5.0) is True
        assert mod_sizes.is_pending(mods) is False
        assert mod_sizes.get_sizes(mods)["ModA"] == _tree_size(mod)

    def test_scan_thread_is_daemon(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mods = tmp_path / "Mods"
        _make_mod(mods, "ModA")
        release = threading.Event()
        real_scan = mod_sizes._scan_dir

        def slow_scan(path: Path, exclude: frozenset[str]) -> dict[str, int]:
            release.wait(5.0)
            return real_scan(path, exclude)

        monkeypatch.setattr(mod_sizes, "_scan_dir", slow_scan)
        mod_sizes.get_sizes(mods)
        with mod_sizes._lock:
            snap = mod_sizes._snapshots[mod_sizes._key(mods)]
            assert snap.thread is not None
            assert snap.thread.daemon is True
        release.set()
        mod_sizes.wait_for_scan(mods, timeout=5.0)


class TestTtlAndInvalidation:
    def test_ttl_expiry_triggers_rescan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mods = tmp_path / "Mods"
        mod = _make_mod(mods, "ModA", files=1, size=1024)
        mod_sizes.get_sizes(mods)
        mod_sizes.wait_for_scan(mods, timeout=5.0)
        first = mod_sizes.get_sizes(mods)["ModA"]

        (mod / "Defs" / "Nested" / "extra.bin").write_bytes(b"y" * 4096)
        # Still inside the TTL: the last snapshot is served, no rescan.
        assert mod_sizes.get_sizes(mods)["ModA"] == first

        monkeypatch.setattr(mod_sizes, "_TTL_SECONDS", 0.0)
        assert mod_sizes.get_sizes(mods)["ModA"] == first
        mod_sizes.wait_for_scan(mods, timeout=5.0)
        assert mod_sizes.get_sizes(mods)["ModA"] == first + 4096

    def test_invalidate_folder_forces_refresh(self, tmp_path: Path) -> None:
        mods = tmp_path / "Mods"
        mod = _make_mod(mods, "ModA", files=1, size=1024)
        _make_mod(mods, "ModB", files=1, size=1024)
        mod_sizes.get_sizes(mods)
        mod_sizes.wait_for_scan(mods, timeout=5.0)
        assert set(mod_sizes.get_sizes(mods)) == {"ModA", "ModB"}

        (mod / "Defs" / "Nested" / "extra.bin").write_bytes(b"y" * 2048)
        mod_sizes.invalidate("ModA")
        assert "ModA" not in mod_sizes.get_sizes(mods)
        mod_sizes.wait_for_scan(mods, timeout=5.0)
        assert mod_sizes.get_sizes(mods)["ModA"] == _tree_size(mod)

    def test_invalidate_all_clears_snapshot(self, tmp_path: Path) -> None:
        mods = tmp_path / "Mods"
        _make_mod(mods, "ModA")
        mod_sizes.get_sizes(mods)
        mod_sizes.wait_for_scan(mods, timeout=5.0)
        assert mod_sizes.get_sizes(mods)

        mod_sizes.invalidate()
        assert mod_sizes.get_sizes(mods) == {}
        assert mod_sizes.wait_for_scan(mods, timeout=5.0) is True
        assert mod_sizes.get_sizes(mods)

    def test_invalidate_during_scan_restarts_and_discards_the_old_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scan running when invalidate() lands must not block the next scan.

        The detached worker is still walking the tree; when it finishes it is
        stale (its generation no longer matches) and must be thrown away rather
        than overwrite the fresh snapshot.
        """
        mods = tmp_path / "Mods"
        mod = _make_mod(mods, "ModA", files=1, size=1024)
        first_started = threading.Event()
        release_first = threading.Event()
        real_scan = mod_sizes._scan_dir
        call_count = 0

        def scan(path: Path, exclude: frozenset[str]) -> dict[str, int]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_started.set()
                assert release_first.wait(5.0), "test never released the first scan"
                return {"Stale": 123}
            return real_scan(path, exclude)

        monkeypatch.setattr(mod_sizes, "_scan_dir", scan)

        mod_sizes.get_sizes(mods)
        assert first_started.wait(2.0), "first scan never started"
        with mod_sizes._lock:
            snap = mod_sizes._snapshots[mod_sizes._key(mods)]
            generation = snap.generation
            first_thread = snap.thread
        assert first_thread is not None

        mod_sizes.invalidate()
        with mod_sizes._lock:
            snap = mod_sizes._snapshots[mod_sizes._key(mods)]
            assert snap.generation == generation + 1
            # The in-flight scan must be detached, not left looking active.
            assert snap.scanning is False
            assert snap.thread is None

        try:
            # The next read starts a second scan even though the first worker
            # is still blocked inside _scan_dir.
            assert mod_sizes.get_sizes(mods) == {}
            assert mod_sizes.wait_for_scan(mods, timeout=5.0) is True
            assert call_count == 2, "invalidate() did not start a new scan"
            assert set(mod_sizes.get_sizes(mods)) == {"ModA"}
        finally:
            release_first.set()

        first_thread.join(5.0)
        assert not first_thread.is_alive()
        sizes = mod_sizes.get_sizes(mods)
        assert set(sizes) == {"ModA"}
        assert sizes["ModA"] == _tree_size(mod)


class TestDirectorySelection:
    def test_missing_dir_is_empty_and_not_pending(self, tmp_path: Path) -> None:
        missing = tmp_path / "Nope"
        assert mod_sizes.get_sizes(missing) == {}
        assert mod_sizes.is_pending(missing) is False
        assert mod_sizes.wait_for_scan(missing, timeout=5.0) is True
        assert mod_sizes.size_mb(missing, "ModA") == 0.0

    def test_non_mod_dirs_and_excluded_dirs_are_ignored(self, tmp_path: Path) -> None:
        mods = tmp_path / "Mods"
        _make_mod(mods, "RealMod", files=1, size=1024)
        plain = mods / "loose_files"
        plain.mkdir()
        (plain / "junk.bin").write_bytes(b"z" * 4096)

        # A backup dir is a valid-looking mod only in this synthetic test —
        # the explicit exclude must still keep it out of the snapshot.
        backup = _make_mod(mods, "_backups", files=1, size=8192)

        mod_sizes.get_sizes(mods, exclude={"_backups"})
        mod_sizes.wait_for_scan(mods, timeout=5.0)
        sizes = mod_sizes.get_sizes(mods)
        assert set(sizes) == {"RealMod"}
        assert sizes["RealMod"] == _tree_size(mods / "RealMod")
        assert _tree_size(backup) > 0
