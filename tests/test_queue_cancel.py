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
        # A pending item has no in-flight download, so it must leave no marker
        # in _cancelled: a stale marker would cancel the next download of the
        # same id (DL-5). Removal itself already guarantees it cannot run.
        assert "123" not in q._cancelled

    def test_cancelled_before_batch_runs_short_circuits(self, tmp_path: Path):
        """A pending item removed before its batch starts never downloads.

        The worker may already hold a reference when remove() pops the item
        (batch selected, then the user hit ✕): the batch must drop that stale
        reference instead of downloading it or resurrecting it as cancelled.
        """
        q = DownloadQueue()
        item = QueueItem(id="123")
        q.items.append(item)
        q.remove("123")  # popped while a batch may still reference it
        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)

        with patch("rwmod.queue.download_batch", return_value={}) as mock_dl:
            _run_batch(q, cfg, item)

        mock_dl.assert_not_called()
        assert item not in q.items
        assert item.status == "pending"  # untouched, not resurrected
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

    def test_cancel_one_item_requeues_unfinished_sibling(self, tmp_path: Path):
        """P1-1: cancelling 111 must not fail its batch sibling 222."""
        from rwmod.downloader import BatchResult, DownloadOutcome
        from rwmod.steamcmd import ErrorKind

        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)
        q = DownloadQueue()
        q.add(["111", "222"])
        items = list(q.items)

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            q.remove("111")  # user clicks ✕ while the shared process is running
            return BatchResult(
                {mid: False for mid in ids},
                {
                    mid: DownloadOutcome(False, ErrorKind.CANCELLED, "SteamCMD 批次被取消")
                    for mid in ids
                },
            )

        with patch("rwmod.queue.download_batch", side_effect=fake_batch):
            asyncio.run(q._process_batch(cfg, items, force=False))

        by_id = {i.id: i for i in q.items}
        assert by_id["111"].status == "cancelled"
        assert by_id["222"].status == "pending"
        assert by_id["222"].retry_count == 1
        assert by_id["222"].msg == "同批取消，已重新排队"
        assert "111" not in q._cancelled  # marker consumed, no leak

    def test_completed_sibling_is_installed_not_requeued(self, tmp_path: Path):
        from rwmod.downloader import BatchResult, DownloadOutcome
        from rwmod.steamcmd import ErrorKind

        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)
        q = DownloadQueue()
        q.add(["111", "222"])
        items = list(q.items)

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            q.remove("111")
            return BatchResult(
                {"111": False, "222": True},
                {
                    "111": DownloadOutcome(False, ErrorKind.CANCELLED, "cancelled"),
                    "222": DownloadOutcome(True, ErrorKind.OK, ""),
                },
            )

        with patch("rwmod.queue.download_batch", side_effect=fake_batch):
            asyncio.run(q._process_batch(cfg, items, force=False))

        by_id = {i.id: i for i in q.items}
        assert by_id["222"].status == "done"
        assert by_id["222"].error_kind == ""

    def test_collateral_cancel_beyond_retry_limit_fails(self, tmp_path: Path):
        from rwmod.downloader import BatchResult, DownloadOutcome
        from rwmod.queue import MAX_INTERRUPT_RETRIES
        from rwmod.steamcmd import ErrorKind

        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)
        q = DownloadQueue()
        q.add(["222"])
        item = q.items[0]
        item.retry_count = MAX_INTERRUPT_RETRIES  # already used up its requeues

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            return BatchResult(
                {mid: False for mid in ids},
                {
                    mid: DownloadOutcome(False, ErrorKind.CANCELLED, "SteamCMD 批次被取消")
                    for mid in ids
                },
            )

        with patch("rwmod.queue.download_batch", side_effect=fake_batch):
            _run_batch(q, cfg, item)

        assert item.status == "failed"
        assert item.msg == "同批取消后重试仍未完成"
        assert item.retry_count == MAX_INTERRUPT_RETRIES + 1

    def test_retry_failed_resets_all_failed_items(self):
        from rwmod.steamcmd import ErrorKind

        q = DownloadQueue()
        q.add(["111", "222"])
        for item in q.items:
            item.status = "failed"
            item.retry_count = 1
            item.error_kind = ErrorKind.UNKNOWN
            item.error_detail = "boom"

        assert q.retry_failed() == 2
        for item in q.items:
            assert item.status == "pending"
            assert item.retry_count == 0
            assert item.error_kind == ""
            assert item.error_detail == ""

    def test_retry_failed_only_selected_ids(self):
        q = DownloadQueue()
        q.add(["111", "222"])
        q.items[0].status = "failed"
        q.items[1].status = "failed"

        assert q.retry_failed(["111"]) == 1
        assert q.items[0].status == "pending"
        assert q.items[1].status == "failed"

    def test_retry_with_no_failed_items_is_zero(self):
        q = DownloadQueue()
        q.add(["111"])
        assert q.retry_failed() == 0
        assert q.items[0].status == "pending"  # untouched, not an error

    def test_pause_stops_new_batches_until_resume(self, tmp_path: Path):
        import contextlib

        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)
        q = DownloadQueue()
        q.add(["111"])
        q.pause()
        assert q.is_paused is True

        calls: list[list[str]] = []

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            calls.append(list(ids))
            return {mid: True for mid in ids}

        async def scenario() -> None:
            # start() creates the worker and captures the loop so resume()'s
            # wake-up actually reaches it.
            await q.start(cfg)
            await asyncio.sleep(0.15)
            # Paused: the pending item is kept but no batch is dispatched.
            assert q.items[0].status == "pending"
            assert calls == []

            q.resume()
            assert q.is_paused is False
            for _ in range(300):
                await asyncio.sleep(0.01)
                if q.items[0].status == "done":
                    break
            q.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await q.wait_stopped(2)

        with patch("rwmod.queue.download_batch", side_effect=fake_batch):
            asyncio.run(scenario())

        assert q.items[0].status == "done"
        assert calls == [["111"]]

    def test_cancelled_target_can_be_readded_after_batch(self, tmp_path: Path):
        """The cancelled marker is in-flight only — a fresh add must download."""
        from rwmod.downloader import BatchResult, DownloadOutcome
        from rwmod.steamcmd import ErrorKind

        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path)
        q = DownloadQueue()
        q.add(["111", "222"])
        items = list(q.items)

        def cancelling_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            q.remove("111")
            return BatchResult(
                {mid: False for mid in ids},
                {mid: DownloadOutcome(False, ErrorKind.CANCELLED, "cancelled") for mid in ids},
            )

        with patch("rwmod.queue.download_batch", side_effect=cancelling_batch):
            asyncio.run(q._process_batch(cfg, items, force=False))
        assert "111" not in q._cancelled

        added = q.add(["111"])
        assert len(added) == 1
        with patch("rwmod.queue.download_batch", return_value={"111": True}):
            _run_batch(q, cfg, added[0])
        assert added[0].status == "done"


# ── Wave 6b: batches never mix instances ───────────────────────────


class TestQueueInstanceBatching:
    @staticmethod
    def _multi_cfg(tmp_path: Path) -> Config:
        from rwmod.config import InstanceConfig

        exe = tmp_path / "steamcmd.exe"
        instances = []
        for iid in ("default", "alpha", "beta"):
            mods = tmp_path / f"Mods_{iid}"
            mods.mkdir()
            instances.append(
                InstanceConfig(
                    id=iid,
                    steamcmd_path=exe,
                    mods_dir=mods,
                    rimworld_dir=tmp_path / f"RW_{iid}",
                    backup_dir=tmp_path / f"Bak_{iid}",
                )
            )
        return Config(instances=instances, active_instance="default")

    def test_worker_keeps_instances_in_separate_batches(self, tmp_path: Path, monkeypatch):
        import contextlib

        cfg = self._multi_cfg(tmp_path)
        q = DownloadQueue()
        assert q.add(["111"], instance_id="alpha")[0].instance_id == "alpha"
        assert q.add(["222"], instance_id="beta")[0].instance_id == "beta"
        seen: list[tuple[str, str, tuple[str, ...]]] = []

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            seen.append((config.active_instance, str(config.mods_dir), tuple(ids)))
            return {mid: True for mid in ids}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)

        async def scenario() -> None:
            await q.start(cfg)
            for _ in range(400):
                await asyncio.sleep(0.02)
                if all(i.status == "done" for i in q.items):
                    break
            q.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await q.wait_stopped(2)

        asyncio.run(asyncio.wait_for(scenario(), timeout=15))

        by_instance = {entry[0]: entry for entry in seen}
        assert set(by_instance) == {"alpha", "beta"}
        assert by_instance["alpha"][1] == str(tmp_path / "Mods_alpha")
        assert by_instance["alpha"][2] == ("111",)
        assert by_instance["beta"][1] == str(tmp_path / "Mods_beta")
        assert by_instance["beta"][2] == ("222",)

    def test_same_mod_id_per_instance_is_never_mixed_in_one_batch(
        self, tmp_path: Path, monkeypatch
    ):
        import contextlib

        cfg = self._multi_cfg(tmp_path)
        q = DownloadQueue()
        q.add(["111"], instance_id="alpha")
        q.add(["111"], instance_id="beta")
        seen: list[tuple[str, tuple[str, ...]]] = []

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            seen.append((config.active_instance, tuple(ids)))
            return {mid: True for mid in ids}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)

        async def scenario() -> None:
            await q.start(cfg)
            for _ in range(400):
                await asyncio.sleep(0.02)
                if all(i.status == "done" for i in q.items):
                    break
            q.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await q.wait_stopped(2)

        asyncio.run(asyncio.wait_for(scenario(), timeout=15))

        assert sorted(seen) == [("alpha", ("111",)), ("beta", ("111",))]
        assert q.items[0].instance_id == "alpha"
        assert q.items[1].instance_id == "beta"

    def test_add_records_the_current_instance(self, monkeypatch):
        import rwmod.queue as q_mod

        monkeypatch.setattr(q_mod, "current_instance_id", lambda: "alpha")
        q = DownloadQueue()
        assert q.add(["333"])[0].instance_id == "alpha"
        assert q.add(["444"], instance_id="beta")[0].instance_id == "beta"

    def test_unknown_instance_fails_closed_instead_of_wrong_download(
        self, tmp_path: Path, monkeypatch
    ):
        """A row whose instance was deleted must fail, not download elsewhere."""
        import contextlib

        cfg = self._multi_cfg(tmp_path)
        q = DownloadQueue()
        q.add(["111"], instance_id="ghost")
        calls: list[list[str]] = []

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            calls.append(list(ids))
            return {mid: True for mid in ids}

        monkeypatch.setattr("rwmod.queue.download_batch", fake_batch)

        async def scenario() -> None:
            await q.start(cfg)
            for _ in range(400):
                await asyncio.sleep(0.02)
                if all(i.status in ("done", "failed") for i in q.items):
                    break
            q.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await q.wait_stopped(2)

        asyncio.run(asyncio.wait_for(scenario(), timeout=15))

        assert calls == []
        assert q.items[0].status == "failed"
        assert "实例不存在" in q.items[0].msg
