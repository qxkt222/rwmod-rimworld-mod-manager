"""Queue expansion tests — auto-collected extras must not loop.

A collection or dependency graph can contain a cycle (A → B → A). The worker
requeues collection children / dependencies it discovers, so without a
per-episode guard the same ids are expanded and "downloaded" forever.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.config import Config
from rwmod.queue import DownloadQueue, QueueItem


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path):
    from rwmod.database import close_db

    close_db()
    with patch("rwmod.database.DB_PATH", tmp_path / "queue_expand.db"):
        yield
        close_db()


def _cfg(tmp_path: Path) -> Config:
    return Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)


def _run_batch(q: DownloadQueue, cfg: Config, item: QueueItem) -> None:
    asyncio.run(q._process_batch(cfg, [item], force=False))


def test_self_referencing_extra_is_not_requeued(tmp_path: Path):
    q = DownloadQueue()
    item = QueueItem(id="A")
    q.items.append(item)
    calls: list[list[str]] = []

    def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
        calls.append(list(ids))
        if extra_out is not None:
            extra_out.append("A")  # cycle back to the item just downloaded
        return {mid: True for mid in ids}

    with patch("rwmod.queue.download_batch", side_effect=fake_batch):
        _run_batch(q, _cfg(tmp_path), item)

    assert calls == [["A"]]
    assert [i.id for i in q.items] == ["A"]


def test_new_extra_is_queued_once(tmp_path: Path):
    q = DownloadQueue()
    item = QueueItem(id="A")
    q.items.append(item)

    def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
        if extra_out is not None:
            extra_out.extend(["B", "B"])  # duplicate children, deduped by add()
        return {mid: True for mid in ids}

    with patch("rwmod.queue.download_batch", side_effect=fake_batch):
        _run_batch(q, _cfg(tmp_path), item)

    assert [i.id for i in q.items] == ["A", "B"]
    assert q.items[1].status == "pending"
