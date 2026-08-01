"""Tests for queue cancellation semantics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.config import Config
from rwmod.queue import DownloadQueue, QueueItem


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path):
    """Keep queue persistence out of the real user DB (~/.rwmod.db).

    _download_one persists each state change via SQLite; without isolation
    those writes would leak into the user's real database and corrupt other
    tests (e.g. queue-persistence counts).
    """
    from rwmod.database import close_db

    close_db()  # drop any stale shared connection first
    with patch("rwmod.database.DB_PATH", tmp_path / "queue_test.db"):
        yield
        close_db()


class TestQueueCancel:
    def test_remove_downloading_marks_cancelled(self):
        q = DownloadQueue()
        item = QueueItem(id="123", status="downloading")
        q.items.append(item)
        assert q.remove("123")
        assert item.status == "cancelled"
        assert "123" in q._cancelled

    def test_remove_pending_pops_item(self):
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        assert q.remove("123")
        assert item not in q.items
        assert "123" in q._cancelled

    def test_cancelled_before_start_short_circuits(self, tmp_path: Path):
        """A pending item removed before its task runs never downloads."""
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        q.remove("123")  # popped + added to _cancelled
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)

        async def _run() -> None:
            await q._download_one(cfg, item, force=False)

        with patch("rwmod.queue.download_one", return_value=True) as mock_dl:
            asyncio.run(_run())
        mock_dl.assert_not_called()
        assert "123" not in q._cancelled

    def test_cancelled_during_download_keeps_cancelled(self, tmp_path: Path):
        """If remove() happens mid-flight, the status must not flip back."""
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)

        def _cancel_during_download(*args: object, **kwargs: object) -> bool:
            q._cancelled.add("123")
            return True

        async def _run() -> None:
            await q._download_one(cfg, item, force=False)

        with patch("rwmod.queue.download_one", side_effect=_cancel_during_download):
            asyncio.run(_run())

        assert item.status == "cancelled"
        assert "123" not in q._cancelled

    def test_normal_download_still_succeeds(self, tmp_path: Path):
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)

        async def _run() -> None:
            await q._download_one(cfg, item, force=False)

        with patch("rwmod.queue.download_one", return_value=True):
            asyncio.run(_run())

        assert item.status == "done"
        assert "123" not in q._cancelled
