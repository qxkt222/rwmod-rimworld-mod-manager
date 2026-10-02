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

    Every state change is persisted via SQLite; without isolation those writes
    would leak into the user's real database and corrupt other tests (e.g.
    queue-persistence counts).
    """
    from rwmod.database import close_db

    close_db()  # drop any stale shared connection first
    with patch("rwmod.database.DB_PATH", tmp_path / "queue_test.db"):
        yield
        close_db()


def _run_batch(q: DownloadQueue, cfg: Config, item: QueueItem) -> None:
    """Drive one batch through the path the persistent worker actually calls.

    ``_process_batch`` is private, but it is the live batch executor — the
    worker loop's only entry point. The obvious public alternative (start() plus
    sleep-until-settled) is timing-dependent and flaky, and a parallel per-item
    implementation used to exist purely to make these tests easy to write; it
    drifted out of the production path and these tests kept passing against it.
    """
    asyncio.run(q._process_batch(cfg, [item], force=False))


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

    def test_cancelled_before_batch_runs_short_circuits(self, tmp_path: Path):
        """A pending item removed before its batch starts never downloads."""
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        q.remove("123")  # popped + added to _cancelled
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)

        with patch("rwmod.queue.download_batch", return_value={}) as mock_dl:
            _run_batch(q, cfg, item)

        mock_dl.assert_not_called()
        assert item.status == "cancelled"
        assert "123" not in q._cancelled

    def test_cancelled_during_download_keeps_cancelled(self, tmp_path: Path):
        """If remove() happens mid-flight, the status must not flip back."""
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)

        def _cancel_during_download(*args: object, **kwargs: object) -> dict[str, bool]:
            q._cancelled.add("123")
            return {"123": True}

        with patch("rwmod.queue.download_batch", side_effect=_cancel_during_download):
            _run_batch(q, cfg, item)

        assert item.status == "cancelled"
        assert "123" not in q._cancelled

    def test_normal_download_still_succeeds(self, tmp_path: Path):
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)

        with patch("rwmod.queue.download_batch", return_value={"123": True}):
            _run_batch(q, cfg, item)

        assert item.status == "done"
        assert "123" not in q._cancelled
