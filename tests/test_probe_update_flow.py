"""Probe: one-click update flow forensics (H1-H4).

Scope: the path
``updates.ts -> POST /api/auto-update/run -> AutoUpdateManager.run_check()
-> queue.add(outdated_ids) -> queue.start(force=True) -> worker -> download_batch``
and its sibling, the per-mod ``⬇ 更新`` button (``POST /api/queue/add`` only).

Hypotheses pinned here:
  H1  a persisted ``downloading`` row is loaded verbatim on restart, the worker
      only drains ``pending``, and ``add()`` dedupes against ``downloading`` --
      so a repeated one-click update silently re-queues nothing (DL-4).
  H2  ``remove()`` leaves the id in the in-memory ``_cancelled`` set, so a
      re-added mod is cancelled by the next ``_process_batch`` (DL-5).
  H3  ``force`` is propagated to each batch item correctly, but it is a single
      queue-wide field on ``DownloadQueue``: a later ``start(force=False)``
      overwrites the force=True set by run_check(), and the per-mod update
      button never forces at all (so an installed mod is skipped as installed).
  H4  the queue path never writes ``download_history`` (E2E-5).

Isolation follows tests/conftest.py and tests/test_probe_data.py: close_db()
first, then repoint DB_PATH at a tmp file. The real ~/.rwmod.db and the real
Mods directory are never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rwmod.config import Config
from rwmod.database import (
    get_download_history,
    init_db,
    queue_load_all,
    queue_upsert,
)
from rwmod.models.schemas import ModIdsRequest
from rwmod.queue import DownloadQueue

# ── isolation fixture ──────────────────────────────────────────────


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Redirect DB/config/queue into tmp_path (never the real user paths)."""
    import rwmod.config as cfg_mod
    import rwmod.database as db_mod
    import rwmod.queue as q_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    db_mod.close_db()  # drop any connection bound to the previous DB_PATH
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "probe_update_flow.db")
    monkeypatch.setattr(db_mod, "_initialized", False)
    monkeypatch.setattr(cfg_mod.Config, "CONFIG_PATH", home / ".rwmod.toml")
    cfg_mod.Config._cache = None
    cfg_mod.Config._cache_ts = 0.0
    cfg_mod.Config._cache_path_key = None
    q_mod._queue = None

    init_db()

    mods_dir = tmp_path / "Mods"
    mods_dir.mkdir()
    cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=mods_dir)
    cfg.backup_dir = tmp_path / "Backups"  # never the default D:/RimWorld/Mods/_backups

    ns = SimpleNamespace(tmp=tmp_path, home=home, db=db_mod, cfg=cfg, mods_dir=mods_dir)
    yield ns
    db_mod.close_db()
    q_mod._queue = None


# ── helpers ────────────────────────────────────────────────────────


def _install_mod(mods_dir: Path, folder: str, wid: str) -> Path:
    """Create a minimal installed mod so _find_existing() resolves it."""
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{folder}</name><packageId>test.{folder}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    (about / "PublishedFileId.txt").write_text(wid, encoding="utf-8")
    return mods_dir / folder


def _fake_batch(sink: list[dict]):
    """download_batch stub that records (ids, force) and reports success."""

    def fake(config, ids, force=False, extra_out=None, progress_cb=None):
        sink.append({"ids": list(ids), "force": force})
        return {i: True for i in ids}

    return fake


async def _wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def _drive_worker(q: DownloadQueue, pred, timeout: float = 5.0) -> bool:
    """Run the real persistent worker loop until pred() holds, then stop it."""
    task = asyncio.create_task(q._worker_loop())
    try:
        return await _wait_until(pred, timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _stop_worker(q: DownloadQueue) -> None:
    q.stop()
    worker = q._worker
    if worker is not None and not worker.done():
        with contextlib.suppress(asyncio.CancelledError):
            await worker


def _statuses(q: DownloadQueue) -> dict[str, str]:
    return {i.id: i.status for i in q.items}


# ── H1: stranded 'downloading' residue ─────────────────────────────


def test_restart_converts_downloading_residue_to_pending(env: SimpleNamespace) -> None:
    """A persisted 'downloading' row means the process died mid-download, so
    restart recovery requeues it as pending (DL-4 fix) and the queue panel's
    clear button still cannot remove a pending item."""
    queue_upsert("111", name="Stuck", status="downloading", progress=0.1, msg="downloading")
    q = DownloadQueue()
    q._load_from_db()
    assert [(i.id, i.status) for i in q.items] == [("111", "pending")]
    assert q.items[0].retry_count == 1  # one automatic requeue
    assert queue_load_all()[0]["status"] == "pending"  # recovery is persisted

    q.clear_done()  # the 队列面板 "清空" button
    assert [i.id for i in q.items] == ["111"]
    assert queue_load_all()[0]["status"] == "pending"


def test_interrupted_download_retry_is_bounded(env: SimpleNamespace) -> None:
    """No infinite requeue: an item that was already requeued once and died
    again mid-download fails instead of being requeued forever."""
    queue_upsert("111", name="Stuck", status="downloading", progress=0.1, retry_count=1)
    q = DownloadQueue()
    q._load_from_db()
    assert [(i.id, i.status) for i in q.items] == [("111", "failed")]
    assert queue_load_all()[0]["status"] == "failed"


def test_restart_residue_is_requeued_by_next_update_run(env: SimpleNamespace) -> None:
    """A residue recovered on restart must be schedulable again; otherwise the
    mod can never be updated through the UI (the exact user-visible loop)."""
    queue_upsert("111", name="Stuck", status="downloading", progress=0.1, msg="downloading")
    q = DownloadQueue()
    q._load_from_db()
    assert _statuses(q) == {"111": "pending"}

    added = q.add(["111"])  # what AutoUpdateManager.run_check() calls
    assert added == []  # already requeued by the startup recovery, so deduped
    assert queue_load_all()[0]["status"] == "pending"
    assert _statuses(q) == {"111": "pending"}


def test_worker_requeues_stranded_downloading_item(env: SimpleNamespace) -> None:
    """Run the real worker loop: the residue must eventually be processed."""
    queue_upsert("111", name="Stuck", status="downloading", progress=0.1)
    queue_upsert("222", status="pending")
    q = DownloadQueue()
    q._load_from_db()
    q._config = env.cfg

    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        # Short timeout: the pending item settles in milliseconds and the
        # stranded one can never settle -- no point waiting out the default.
        ok = asyncio.run(
            _drive_worker(q, lambda: all(i.status == "done" for i in q.items), timeout=2.0)
        )
    assert ok
    batch_ids = sorted(sink[0]["ids"]) if sink else []
    assert batch_ids == ["111", "222"]  # the recovered residue is handed over too
    assert queue_load_all()[0]["status"] == "done"


def test_run_check_queued_count_reflects_dedupe(env: SimpleNamespace, monkeypatch) -> None:
    from rwmod.autoupdate import AutoUpdateManager
    from rwmod.queue import get_queue

    queue_upsert("111", name="Stuck", status="downloading", progress=0.1)
    q = get_queue()  # app startup: lifespan calls get_queue() -> _load_from_db()
    # Startup recovery already requeued the interrupted row as pending.
    assert [(i.id, i.status) for i in q.items] == [("111", "pending")]

    monkeypatch.setattr("rwmod.autoupdate.Config.load", lambda: env.cfg)
    monkeypatch.setattr(
        "rwmod.workshop.check_mod_updates",
        lambda mods_dir: {
            "updates": [{"workshop_id": "111", "name": "A"}],
            "partial": False,
            "failed_batches": 0,
            "total_batches": 1,
        },
    )
    sink: list[dict] = []
    monkeypatch.setattr("rwmod.queue.download_batch", _fake_batch(sink))

    async def scenario() -> dict:
        result = await AutoUpdateManager().run_check()
        # Stop the worker before the loop starts draining: this test pins the
        # reported count and the item state, not the download itself.
        q.stop()
        return result

    result = asyncio.run(scenario())

    assert result["outdated"] == 1
    assert result["queued"] == 0  # nothing newly added: the id was already requeued
    assert [i.status for i in q.items] == ["pending"]
    assert q.items[0].force is True  # start(force=True) marked it as an update


def test_worker_drains_pending_items(env: SimpleNamespace) -> None:
    """Positive control for the worker: plain pending items are downloaded."""
    q = DownloadQueue()
    q.add(["111", "222"])
    q._config = env.cfg

    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        ok = asyncio.run(_drive_worker(q, lambda: all(i.status == "done" for i in q.items)))
    assert ok
    assert sorted(sink[0]["ids"]) == ["111", "222"]
    assert {r["workshop_id"]: r["status"] for r in queue_load_all()} == {
        "111": "done",
        "222": "done",
    }


def test_failed_item_snapshot_exposes_structured_error(env: SimpleNamespace) -> None:
    """P0-2: the queue snapshot must carry the real failure reason.

    Before this, every failure surfaced the hard-coded
    "下载失败（含 Skymods 备用源）" no matter what actually happened.
    """
    from rwmod.downloader import BatchResult, DownloadOutcome
    from rwmod.steamcmd import ErrorKind

    q = DownloadQueue()
    q.add(["111"])
    items = list(q.items)

    def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
        return BatchResult(
            {mid: False for mid in ids},
            {
                mid: DownloadOutcome(
                    ok=False,
                    error_kind=ErrorKind.FILE_NOT_FOUND,
                    error_detail="SteamCMD: File Not Found",
                )
                for mid in ids
            },
        )

    with patch("rwmod.queue.download_batch", side_effect=fake_batch):
        asyncio.run(q._process_batch(env.cfg, items, force=False))

    snap = {i["id"]: i for i in q.snapshot()}["111"]
    assert snap["status"] == "failed"
    assert snap["error_kind"] == ErrorKind.FILE_NOT_FOUND
    assert snap["error_detail"] == "SteamCMD: File Not Found"
    # Short, human-readable reason — no log dump in the queue/WS payload.
    assert snap["msg"] == ErrorKind.explain(ErrorKind.FILE_NOT_FOUND)
    assert len(snap["msg"]) < 60


# ── H2: _cancelled pollution ───────────────────────────────────────


def test_readd_after_cancel_downloads_normally(env: SimpleNamespace) -> None:
    q = DownloadQueue()
    q.add(["111"])
    assert q.remove("111") is True
    assert "111" not in q._cancelled  # nothing was in flight, no marker needed

    added = q.add(["111"])  # user clicks 一键更新 again
    assert len(added) == 1

    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        asyncio.run(q._process_batch(env.cfg, added, force=True))
    assert added[0].status == "done"  # downloads normally, not instantly cancelled
    assert sink and sink[0]["ids"] == ["111"]


def test_add_force_upgrades_an_already_pending_item(env: SimpleNamespace) -> None:
    """POST /queue/add {force:true} on an id already queued must upgrade the
    pending item to an update instead of being ignored."""
    q = DownloadQueue()
    q.add(["111"])
    assert q.add(["111"], force=True) == []  # deduped (already queued)...
    assert q.items[0].force is True  # ...but the update intent was applied


def test_force_intent_survives_restart(env: SimpleNamespace) -> None:
    """A queued update must still replace the installed copy after a restart;
    force is persisted with the item instead of being a queue-wide field."""
    queue_upsert("111", name="Update", status="pending", force=True)
    q = DownloadQueue()
    q._load_from_db()
    assert q.items[0].force is True


def test_cancelled_pollution_is_process_local(env: SimpleNamespace) -> None:
    """_cancelled is an in-memory field: not persisted, and a pending removal
    does not put anything in it in the first place (DL-5 fix)."""
    q = DownloadQueue()
    q.add(["111"])
    q.remove("111")
    assert "111" not in q._cancelled  # pending removal: no in-flight download
    assert queue_load_all() == []  # the pending row was deleted, nothing persisted

    # A fresh process (fresh queue) starts clean -- re-adding then downloads.
    q2 = DownloadQueue()
    q2._load_from_db()
    assert q2._cancelled == set()
    added = q2.add(["111"])
    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        asyncio.run(q2._process_batch(env.cfg, added, force=True))
    assert added[0].status == "done"


def test_pending_removal_does_not_pollute_cancelled(env: SimpleNamespace) -> None:
    """_cancelled only tracks in-flight downloads: removing a pending item
    leaves no marker, and start/stop cycles cannot resurrect one (DL-5 fix)."""
    q = DownloadQueue()
    q.add(["111"])
    q.remove("111")

    async def cycle() -> None:
        await q.start(env.cfg, force=False)
        assert "111" not in q._cancelled
        await _stop_worker(q)

    asyncio.run(cycle())
    assert "111" not in q._cancelled

    added = q.add(["111"])  # re-added after the cycle: still clean
    assert len(added) == 1
    assert "111" not in q._cancelled


# ── H3: force semantics ────────────────────────────────────────────


def test_start_force_true_reaches_batch(env: SimpleNamespace) -> None:
    """run_check()'s start(force=True) reaches the batch through the real worker."""
    q = DownloadQueue()
    q.add(["111", "222"])
    sink: list[dict] = []

    async def scenario() -> bool:
        with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
            await q.start(env.cfg, force=True)
            ok = await _wait_until(lambda: all(i.status == "done" for i in q.items))
            await _stop_worker(q)
        return ok

    assert asyncio.run(scenario()) is True
    assert len(sink) == 1
    assert sink[0]["force"] is True


def test_force_false_skips_installed_without_download(env: SimpleNamespace) -> None:
    """Baseline semantics: force=False means 'install, do not update'."""
    _install_mod(env.mods_dir, "SomeMod", "111")
    q = DownloadQueue()
    q.add(["111"])
    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        asyncio.run(q._process_batch(env.cfg, q.items, force=False))
    assert q.items[0].status == "done"
    assert q.items[0].progress == 1.0
    assert sink == []  # never handed to the downloader


def test_force_true_downloads_installed_mod(env: SimpleNamespace) -> None:
    """Baseline semantics: force=True means 'update/replace what is installed'."""
    _install_mod(env.mods_dir, "SomeMod", "111")
    q = DownloadQueue()
    q.add(["111"])
    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        asyncio.run(q._process_batch(env.cfg, q.items, force=True))
    assert q.items[0].status == "done"
    assert sink and sink[0]["ids"] == ["111"] and sink[0]["force"] is True


def test_later_start_without_force_does_not_strip_update_force(env: SimpleNamespace) -> None:
    q = DownloadQueue()
    q.add(["111"])  # already queued by run_check (outdated mod)
    sink: list[dict] = []

    async def scenario() -> bool:
        with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
            await q.start(env.cfg, force=True)  # AutoUpdateManager.run_check
            await q.start(env.cfg, force=False)  # 队列面板 开始, before the batch runs
            ok = await _wait_until(lambda: all(i.status == "done" for i in q.items))
            await _stop_worker(q)
        return ok

    assert asyncio.run(scenario()) is True
    assert sink and sink[0]["force"] is True  # the item's force was not stripped


def test_single_mod_update_button_path_downloads(env: SimpleNamespace) -> None:
    from rwmod.routers.queue import queue_add

    _install_mod(env.mods_dir, "SomeMod", "111")
    q = DownloadQueue()
    # updates.ts now posts {"ids": [...], "force": true} for ⬇更新.
    # Wave 6b: the endpoint also takes the request's Config (instance owner).
    queue_add(ModIdsRequest(ids=["111"], force=True), env.cfg, q)
    assert len(q.items) == 1

    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        # 队列面板 开始 -> POST /queue/start with no body -> force=False
        asyncio.run(q._process_batch(env.cfg, q.items, force=False))
    assert q.items[0].status == "done"
    assert sink and sink[0]["ids"] == ["111"]  # the installed mod is really re-downloaded


# ── H4: download_history ───────────────────────────────────────────


def test_queue_download_is_recorded_in_history(env: SimpleNamespace) -> None:
    q = DownloadQueue()
    q.add(["111"])
    sink: list[dict] = []
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch(sink)):
        asyncio.run(q._process_batch(env.cfg, q.items, force=True))
    assert q.items[0].status == "done"
    assert [h["workshop_id"] for h in get_download_history()] == ["111"]  # today: []


def test_queue_history_records_the_items_instance(env: SimpleNamespace) -> None:
    """Wave 6b: history rows belong to the instance the item was queued for."""
    q = DownloadQueue()
    q.add(["222"], instance_id="alpha")
    with patch("rwmod.queue.download_batch", side_effect=_fake_batch([])):
        asyncio.run(q._process_batch(env.cfg, q.items, force=True))
    assert q.items[0].status == "done"

    by_instance = get_download_history(instance_id="alpha")
    assert [h["workshop_id"] for h in by_instance] == ["222"]
    assert by_instance[0]["instance_id"] == "alpha"
    assert get_download_history(instance_id="default") == []


def test_same_workshop_id_in_two_instances_persists_and_records_history(
    env: SimpleNamespace,
) -> None:
    """Wave 6c: the composite queue key keeps both instances' rows and history."""
    q = DownloadQueue()
    q.add(["111"], instance_id="alpha")
    q.add(["111"], instance_id="beta")

    reloaded = DownloadQueue()
    reloaded._load_from_db()
    assert {(i.instance_id, i.id) for i in reloaded.items} == {
        ("alpha", "111"),
        ("beta", "111"),
    }

    for item in reloaded.items:
        with patch("rwmod.queue.download_batch", side_effect=_fake_batch([])):
            asyncio.run(reloaded._process_batch(env.cfg, [item], force=True))
        assert item.status == "done"

    assert [h["workshop_id"] for h in get_download_history(instance_id="alpha")] == ["111"]
    assert [h["workshop_id"] for h in get_download_history(instance_id="beta")] == ["111"]
