"""Download queue with concurrency control + SQLite persistence.

Queue survives server restarts: pending items are stored in SQLite
and reloaded on startup. Status changes are written to DB immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from rwmod.config import Config, current_instance_id
from rwmod.downloader import BATCH_SIZE, _find_existing, download_batch, explain_outcome
from rwmod.steamcmd import ErrorKind

__all__ = ["DownloadQueue", "get_queue", "MAX_CONCURRENT"]

_log = logging.getLogger(__name__)

# Concurrent SteamCMD *batches* (each batch = BATCH_SIZE mods in one process).
# Two processes already saturate most home connections; more would only
# contend for Steam's servers and the shared workshop_log.txt.
MAX_CONCURRENT = 2

# How many times an interrupted (process died mid-download) item is requeued
# before it fails instead. Without a bound, a download that reliably kills the
# process would be retried on every restart, forever.
MAX_INTERRUPT_RETRIES = 1


@dataclass
class QueueItem:
    id: str
    name: str = ""
    # Wave 6b: the instance this item belongs to, captured at enqueue time.
    # Empty means "whichever instance the queue was started with" (legacy /
    # directly-constructed items in tests).
    instance_id: str = ""
    status: str = "pending"  # pending | downloading | done | failed | cancelled
    progress: float = 0.0
    msg: str = ""
    # Update intent: replace an already-installed copy instead of skipping it
    # as "已安装". Set by add(force=True) / start(force=True) and persisted, so
    # an update queued before a restart still replaces the installed copy.
    force: bool = False
    # Interrupted-download recoveries that have not reached a terminal state
    # yet. Incremented when a persisted 'downloading' row is recovered;
    # reset when the download settles. Bounded by MAX_INTERRUPT_RETRIES.
    retry_count: int = 0
    # Live download telemetry (SteamCMD byte progress, updated by the reader
    # thread while a batch download is in flight).
    downloaded: float = 0.0  # bytes downloaded so far
    total: float = 0.0  # total bytes (0 while unknown)
    speed_bps: float = 0.0  # smoothed bytes/second
    # Structured failure reason (P0-2): a stable machine code plus a short
    # detail for hover/logs. Both are empty for pending/running/done items.
    # Not persisted yet (no DB columns) — failed rows do not survive restarts
    # anyway, so this is process-local state.
    error_kind: str = ""
    error_detail: str = ""


@dataclass
class DownloadQueue:
    items: list[QueueItem] = field(default_factory=list)
    _running: bool = False
    # P2-1: pause stops *dispatching* new batches. The in-flight batch runs to
    # completion (never killed) and pending items stay in `items` untouched.
    # Process-local by design — not persisted across restarts.
    _paused: bool = False
    _semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT))
    _callbacks: list[Callable] = field(default_factory=list)
    # Workshop IDs whose in-flight download should be treated as cancelled.
    # Only remove()-ing an item that is actually *downloading* adds an id here;
    # the marker is consumed (discarded) by _process_batch at its next
    # checkpoint, and any stale marker is dropped when the item's lifecycle
    # ends (remove/clear_done/add) — the set never grows forever.
    _cancelled: set[str] = field(default_factory=set)
    # (instance_id, workshop id) pairs already dispatched by the worker during
    # the current drain episode. Auto-collected collection children /
    # dependencies are only queued once per episode, so a cyclic collection or
    # dependency graph (A contains B, B contains A) cannot requeue itself
    # forever. Cleared when the queue goes idle, so a later user-initiated run
    # starts fresh. Scoped by instance: the same workshop id may legitimately
    # be queued for two installations.
    _expanded: set[tuple[str, str]] = field(default_factory=set)
    # Persistent background worker that drains pending items as they arrive.
    _worker: asyncio.Task | None = None
    # Wave 6b: Config objects derived per instance id, so one queue can drain
    # items belonging to different RimWorld installations. Cleared in start()
    # (the base config and its instance table may have changed).
    _instance_configs: dict[str, Config] = field(default_factory=dict)
    # In-flight batch tasks (DL-7). Up to MAX_CONCURRENT batches run at once;
    # the worker keeps draining instead of awaiting each batch serially.
    _active: set[asyncio.Task] = field(default_factory=set)
    # (instance_id, workshop id) pairs handed to an in-flight batch that has
    # not flipped them to 'downloading' yet (its task may still be waiting for
    # a semaphore slot). Excluded from the next selection so an id is never
    # double-booked — scoped by instance so alpha's 111 does not block beta's.
    _scheduled: set[tuple[str, str]] = field(default_factory=set)
    _config: Config | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Event loop captured in start(); used to wake the worker from sync
    # (FastAPI thread-pool) contexts via call_soon_threadsafe.
    _loop: asyncio.AbstractEventLoop | None = None
    # Guards items/_cancelled across the thread pool (sync /queue/* endpoints)
    # and the event-loop worker. Field-level mutations of QueueItem are safe
    # under the GIL; only list/set structure is protected.
    _items_lock: threading.Lock = field(default_factory=threading.Lock)

    def _item_instance(self, item: QueueItem) -> str:
        """Instance id for *item*; legacy empty ids follow the queue's config."""
        if item.instance_id:
            return item.instance_id
        return self._config.active_instance if self._config is not None else "default"

    def _config_for(self, instance_id: str) -> Config:
        """Return a Config whose flat properties point at *instance_id*.

        The queue holds one shared ``Config`` (with the full instance table
        from ``start()``). A derived Config is built per instance and cached.
        The worker refuses unknown owners before dispatch (fail closed); the
        active-instance fallback here only covers direct ``_process_batch``
        calls whose items predate per-instance ownership.
        """
        base = self._config
        if base is None:
            return Config()
        if not instance_id or instance_id == base.active_instance:
            return base
        cached = self._instance_configs.get(instance_id)
        if cached is not None:
            return cached
        inst = base.get_instance(instance_id)
        if inst is None:
            _log.warning(
                "队列项引用的实例 %s 不存在，回退到活动实例 %s",
                instance_id,
                base.active_instance,
            )
            return base
        derived = Config(
            steamcmd_path=inst.steamcmd_path,
            mods_dir=inst.mods_dir,
            rimworld_dir=inst.rimworld_dir,
            backup_dir=inst.backup_dir,
            steam_api_key=base.steam_api_key,
            update_backup=base.update_backup,
            backup_keep=base.backup_keep,
            enable_skymods=base.enable_skymods,
            config_dir=inst.config_dir,
            enable_steam_db=base.enable_steam_db,
            steam_db_max_bytes=base.steam_db_max_bytes,
            instances=base.instances,
            active_instance=instance_id,
        )
        self._instance_configs[instance_id] = derived
        return derived

    def add(
        self,
        mod_ids: list[str],
        force: bool = False,
        instance_id: str | None = None,
    ) -> list[QueueItem]:
        """Queue mod ids; returns only the items that were newly created.

        ``instance_id`` defaults to the currently active instance and is
        recorded on every new item: the worker then resolves that instance's
        own mods directory instead of whatever instance is active later.
        Ids already queued as pending/downloading *for the same instance* are
        deduped (a pending item is upgraded to ``force`` when the caller asks
        for an update). ``force`` marks the download as "replace the installed
        copy".
        """
        iid = instance_id or current_instance_id()
        new_items: list[QueueItem] = []
        upgraded: list[QueueItem] = []
        with self._items_lock:
            for mid in mod_ids:
                already = next(
                    (
                        i
                        for i in self.items
                        if i.id == mid
                        and (i.instance_id or iid) == iid
                        and i.status in ("pending", "downloading")
                    ),
                    None,
                )
                if already is not None:
                    # A downloading item already passed its force decision;
                    # only a still-pending one can be upgraded.
                    if force and already.status == "pending" and not already.force:
                        already.force = True
                        upgraded.append(already)
                    continue
                # Any cancel marker left over from this id's previous life is
                # meaningless for a freshly queued item — drop it so re-adding
                # can never be cancelled by stale state (DL-5).
                self._cancelled.discard(mid)
                item = QueueItem(id=mid, force=force, instance_id=iid)
                self.items.append(item)
                new_items.append(item)
        for item in [*new_items, *upgraded]:
            self._persist(item)
        # Wake the idle worker immediately instead of waiting out its 30s
        # sleep timeout — add() may run on a thread-pool thread.
        self._wake_worker()
        return new_items

    def _wake_worker(self) -> None:
        """Wake the persistent worker from any thread (sync endpoint or loop)."""
        loop, worker = self._loop, self._worker
        if loop is not None and worker is not None and not worker.done():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._wake.set)

    def remove(self, mod_id: str) -> bool:
        """Remove a queue item (or cancel an in-flight download).

        For a *pending* item it is removed entirely; a *downloading* item is
        marked cancelled and kept in the list so the UI shows the cancellation.
        The in-flight SteamCMD subprocess is killed (see steamcmd.cancel_download)
        so the download actually stops instead of running to completion; the
        task is additionally marked cancelled so its status is never flipped
        back to done/failed afterwards.

        Only a downloading item leaves a marker in ``_cancelled`` — that set
        means "an in-flight download must be discarded", so anything else
        (pending/failed/done) must not pollute it (DL-5).
        """
        cancelled_item: QueueItem | None = None
        removed_item: QueueItem | None = None
        with self._items_lock:
            for i, item in enumerate(self.items):
                if item.id == mod_id:
                    if item.status == "downloading":
                        self._cancelled.add(mod_id)
                        item.status = "cancelled"
                        item.msg = "已取消（正在终止 SteamCMD）"
                        item.error_kind = ""
                        item.error_detail = ""
                        cancelled_item = item
                    else:
                        # No in-flight download to discard: drop any stale
                        # marker so the id starts clean if it is re-added.
                        self._cancelled.discard(mod_id)
                        self.items.pop(i)
                        removed_item = item
                    break
            else:
                return False
        if cancelled_item is not None:
            self._persist(cancelled_item)
            # Kill the live SteamCMD process, if any — best effort.
            try:
                from rwmod.steamcmd import cancel_download

                cancel_download(mod_id)
            except Exception:  # noqa: BLE001 — cancel must never fail the removal
                pass
        elif removed_item is not None:
            # Since v4 the row key is (instance, workshop id): only delete the
            # removed item's own row, never another instance's same-id row.
            self._db_delete(
                removed_item.id,
                removed_item.instance_id or self._item_instance(removed_item),
            )
        return True

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop dispatching new batches; an in-flight batch finishes normally."""
        self._paused = True

    def resume(self) -> None:
        """Allow dispatching again and wake the worker if it is idle."""
        self._paused = False
        self._wake_worker()

    def retry_failed(self, mod_ids: list[str] | None = None) -> int:
        """Move failed items back to pending (all, or only ``mod_ids``).

        Returns how many were requeued. Zero is a valid no-op, not an error —
        callers answer 200 with ``accepted=0``.
        """
        wanted = set(mod_ids) if mod_ids else None
        accepted: list[QueueItem] = []
        with self._items_lock:
            for item in self.items:
                if item.status != "failed":
                    continue
                if wanted is not None and item.id not in wanted:
                    continue
                item.status = "pending"
                item.progress = 0.0
                item.retry_count = 0
                item.msg = "重新排队"
                item.error_kind = ""
                item.error_detail = ""
                accepted.append(item)
        for item in accepted:
            self._persist(item)
        if accepted:
            self._wake_worker()
        return len(accepted)

    def clear_done(self) -> None:
        with self._items_lock:
            done_ids = [i.id for i in self.items if i.status in ("done", "cancelled")]
            self.items = [i for i in self.items if i.status not in ("done", "cancelled")]
            for wid in done_ids:
                # Lifecycle end: no checkpoint will consume these markers.
                self._cancelled.discard(wid)
        for wid in done_ids:
            self._db_delete(wid)
        self._db_clear_done()

    def on_update(self, cb: Callable) -> None:
        self._callbacks.append(cb)

    async def _notify(self) -> None:
        snapshot = self.snapshot()
        for cb in self._callbacks:
            with contextlib.suppress(Exception):
                await cb(snapshot)

    def snapshot(self) -> list[dict]:
        with self._items_lock:
            items = list(self.items)
        return [
            {
                "id": i.id,
                "name": i.name,
                "instance_id": i.instance_id,
                "status": i.status,
                "progress": i.progress,
                "msg": i.msg,
                "downloaded": i.downloaded,
                "total": i.total,
                "speed_bps": i.speed_bps,
                "error_kind": i.error_kind,
                "error_detail": i.error_detail,
            }
            for i in items
        ]

    async def start(self, config: Config, force: bool = False) -> None:
        """Start (or resume) draining the queue.

        Uses a single persistent worker task so that items added *while* the
        queue is already running are picked up too — the old implementation
        returned early when ``_running`` was True, silently dropping any
        pending items added after the first ``start()`` call.

        ``force=True`` (the one-click update path) marks every *pending* item
        as an update. Force is stored per item, not on the queue, so a later
        ``start(force=False)`` (队列面板 开始) can neither strip an update's
        force nor leak force onto unrelated installs (DL-11).
        """
        async with self._lock:
            self._config = config
            # Derived per-instance configs may be stale if the instance table
            # or the flat settings changed since the last start().
            self._instance_configs.clear()
            if force:
                with self._items_lock:
                    upgraded = [i for i in self.items if i.status == "pending" and not i.force]
                    for item in upgraded:
                        item.force = True
                for item in upgraded:
                    self._persist(item)
            self._loop = asyncio.get_running_loop()
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._worker_loop())
            self._wake.set()

    def stop(self) -> None:
        """Cancel the persistent worker and every in-flight batch."""
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
        for task in list(self._active):
            task.cancel()

    async def wait_stopped(self, timeout: float = 3.0) -> None:
        """Await the worker task after stop(), bounding the shutdown wait.

        The in-flight SteamCMD download runs in a thread and may outlive the
        event loop; we only need the worker task itself to finish so it stops
        draining and writing to the DB before close_db().
        """
        worker = self._worker
        if worker is None or worker.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
        except TimeoutError:
            _log.warning("队列 worker 在 %ss 内未停止", timeout)
        except asyncio.CancelledError:
            # stop() cancels the worker; awaiting that cancelled task re-raises
            # its CancelledError here. Swallow it — shutdown must proceed to
            # close_db() — but only when the cancellation is the worker's: if
            # *this* coroutine is being cancelled (caller/loop teardown), the
            # signal has to keep propagating.
            current = asyncio.current_task()
            if (current is not None and current.cancelling()) or not worker.cancelled():
                raise
            _log.debug("队列 worker 已停止（任务已被取消）")

    async def _worker_loop(self) -> None:
        """Drain pending items forever until the queue is empty and idle.

        Items are consumed in batches: up to BATCH_SIZE pending ids are
        handed to download_batch(), which downloads them in ONE SteamCMD
        process (one login) instead of starting a process per mod. Up to
        MAX_CONCURRENT batches run concurrently (each as its own task), so
        the worker keeps pulling items while a batch is downloading. The
        actual SteamCMD execution is still serialized by the global run lock
        in steamcmd.py; the concurrency here covers probe/install/fallback
        work around it and keeps the queue contract truthful.
        """
        self._running = True
        try:
            while True:
                # Reap finished batch tasks before scheduling more.
                self._active = {t for t in self._active if not t.done()}

                if self._paused:
                    # P2-1: pause = stop taking new batches. Pending items stay
                    # in `items` untouched; in-flight batches finish normally.
                    # Wait for resume()/add() to wake us.
                    self._wake.clear()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=30)
                    continue

                # Grab the next pending batch. One batch never mixes:
                #  * force values — download_batch applies a single force flag
                #    to the whole SteamCMD invocation (a forced install would
                #    replace an installed copy the user never asked to update);
                #  * instances — each item resolves its own mods directory and
                #    SteamCMD root, so mixing would download into the wrong
                #    installation.
                # Ids already handed to an in-flight batch are still 'pending'
                # for a moment (their task may be waiting on the semaphore), so
                # they must be excluded explicitly to avoid double-booking.
                batch: list[QueueItem] = []
                with self._items_lock:
                    base_iid = self._config.active_instance if self._config is not None else ""
                    pending = [
                        i
                        for i in self.items
                        if i.status == "pending"
                        and ((i.instance_id or base_iid), i.id) not in self._scheduled
                    ]
                    if pending:
                        first = pending[0]
                        first_iid = first.instance_id or base_iid
                        batch = [
                            i
                            for i in pending
                            if i.force == first.force and (i.instance_id or base_iid) == first_iid
                        ][:BATCH_SIZE]
                if self._paused:
                    # pause() raced with the batch selection — leave the items
                    # pending and fall back to the wait at the top.
                    continue
                if not batch:
                    if self._active:
                        # Still draining in-flight batches: wait for one to
                        # finish, then look for ids it re-queued (collection
                        # children / dependencies).
                        await asyncio.wait(self._active, return_when=asyncio.FIRST_COMPLETED)
                        continue
                    # Nothing to do — wait for a wake-up (new item or start()).
                    # A drain episode just ended: the next one may re-expand
                    # ids the previous one already handled.
                    self._expanded.clear()
                    self._wake.clear()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=30)
                    # Re-check after wake or timeout.
                    continue

                # start() always sets _config before waking the worker, so it
                # is never None here; guard anyway to satisfy the type checker.
                config = self._config
                if config is None:
                    continue
                if len(self._active) >= MAX_CONCURRENT:
                    # Concurrency budget full — wait for a slot to free up.
                    await asyncio.wait(self._active, return_when=asyncio.FIRST_COMPLETED)
                    continue

                batch_iid = self._item_instance(batch[0])
                base_cfg = self._config
                if (
                    base_cfg is not None
                    and batch_iid != base_cfg.active_instance
                    and base_cfg.get_instance(batch_iid) is None
                ):
                    # Fail closed: the owning instance was deleted while these
                    # rows were pending. Downloading into the active instance
                    # instead would silently write to the wrong installation.
                    _log.error(
                        "队列批次引用了不存在的实例 %s，标记失败: %s",
                        batch_iid,
                        [i.id for i in batch],
                    )
                    for item in batch:
                        item.status = "failed"
                        item.progress = 0
                        item.msg = f"实例不存在: {batch_iid}"
                        item.error_kind = ErrorKind.UNKNOWN
                        item.error_detail = batch_iid
                        self._persist(item)
                        self._record_history(item, "failed")
                    await self._notify()
                    continue

                keys = frozenset((batch_iid, i.id) for i in batch)
                self._scheduled.update(keys)
                batch_cfg = self._config_for(batch_iid)
                task = asyncio.create_task(self._run_batch(batch_cfg, batch, batch[0].force))

                def _release(
                    _task: asyncio.Task[None], booked: frozenset[tuple[str, str]] = keys
                ) -> None:
                    self._scheduled.difference_update(booked)

                task.add_done_callback(_release)
                self._active.add(task)
        finally:
            self._running = False
            tasks = list(self._active)
            self._active.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                # Never let teardown of the batch tasks mask the worker's own
                # cancellation; the downloads run in threads and may outlive
                # the loop exactly like before DL-7.
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*tasks, return_exceptions=True)
            await self._notify()

    async def _run_batch(self, config: Config, batch: list[QueueItem], force: bool) -> None:
        """Run one batch task, rescuing its items if it crashes (DL-3)."""
        try:
            await self._process_batch(config, batch, force)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a crash must never kill the worker
            _log.exception("队列 worker 处理批次异常: %s", [i.id for i in batch])
            with self._items_lock:
                for item in batch:
                    # Rescue items already flipped to 'downloading' too: the
                    # batch selector only ever picks 'pending', so leaving them
                    # downloading strands them exactly like a persisted
                    # residue (DL-3).
                    if item.status in ("pending", "downloading"):
                        item.status = "failed"
                        item.progress = 0
                        item.retry_count = 0
                        item.msg = f"下载异常: {e}"
                        item.error_kind = ErrorKind.UNKNOWN
                        item.error_detail = str(e)
                        self._cancelled.discard(item.id)
            for item in batch:
                self._persist(item)
            await self._notify()

    async def _process_batch(self, config: Config, batch: list[QueueItem], force: bool) -> None:
        """Download one batch of items via a single SteamCMD process.

        The batch belongs to exactly one instance (the worker groups by
        instance); *config* is that instance's derived Config.
        """
        iid = self._item_instance(batch[0]) if batch else config.active_instance
        async with self._semaphore:
            # Per-item force is the source of truth: start() and the worker
            # pass one value for the batch, but an item queued by the ⬇更新
            # button carries its own force=True (DL-12) and must not be
            # downgraded by a later plain 开始 (DL-11).
            force = force or any(i.force for i in batch)

            # Items cancelled while this batch waited for a slot stay cancelled;
            # items removed from the queue are no longer ours to process.
            live: list[QueueItem] = []
            for item in batch:
                with self._items_lock:
                    if not any(i is item for i in self.items):
                        continue  # removed while the batch was being scheduled
                    cancelled = item.id in self._cancelled
                    if cancelled:
                        self._cancelled.discard(item.id)
                if cancelled:
                    item.status = "cancelled"
                    item.msg = "已取消"
                    item.retry_count = 0
                    self._persist(item)
                    continue
                item.status = "downloading"
                item.progress = 0.1
                item.msg = "下载中..."
                self._persist(item)
                live.append(item)
            if live:
                await self._notify()
            if not live:
                return

            # Check already installed (skip unless force)
            ids = [i.id for i in live]
            remaining: list[QueueItem] = []
            for item in live:
                existing = _find_existing(config.mods_dir, item.id, instance_id=iid)
                if existing and not force:
                    item.status = "done"
                    item.progress = 1.0
                    item.name = existing.name
                    item.msg = "已安装"
                    self._persist(item)
                else:
                    if existing and force:
                        item.name = existing.name
                    remaining.append(item)
            if remaining:
                await self._notify()
            live = remaining
            if not live:
                return

            ids = [i.id for i in live]
            # Mark these ids as dispatched for this drain episode so an extra
            # that points back at one of them cannot requeue it forever.
            self._expanded.update((iid, mid) for mid in ids)
            # Live progress from the SteamCMD reader thread: update each
            # item's byte counters + speed, and push a throttled WS snapshot.
            speed_samples: dict[str, tuple[float, float]] = {}
            last_push = [0.0]

            def _progress(mod_id: str, percent: float, downloaded: float, total: float) -> None:
                item = next((i for i in live if i.id == mod_id), None)
                if item is None:
                    return
                item.progress = min(0.95, percent / 100.0)  # keep 5% for install
                item.downloaded = downloaded
                item.total = total
                now = time.monotonic()
                prev = speed_samples.get(mod_id)
                if prev is not None and now - prev[0] >= 0.5:
                    dt = now - prev[0]
                    item.speed_bps = max(0.0, (downloaded - prev[1]) / dt)
                    speed_samples[mod_id] = (now, downloaded)
                else:
                    speed_samples[mod_id] = (now, downloaded)
                item.msg = f"下载中 {percent:.1f}%"
                # Throttle WebSocket pushes to ~1/s — SteamCMD emits progress
                # lines several times per second.
                if now - last_push[0] >= 1.0:
                    last_push[0] = now
                    loop = self._loop
                    if loop is not None:
                        with contextlib.suppress(RuntimeError):
                            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._notify()))

            # Batch download (blocking — runs in a thread). Collection
            # children / new dependencies come back via extra_out and are
            # re-queued below, so they flow through the same batch pipeline.
            extra: list[str] = []
            ok = await asyncio.to_thread(
                download_batch,
                config,
                ids,
                force=force,
                extra_out=extra,
                progress_cb=_progress,
            )
            # BatchResult carries a DownloadOutcome per id; older/plain-dict
            # doubles simply have none and fall back to a generic reason.
            outcome_map = getattr(ok, "outcomes", None) or {}

            for item in live:
                # Cancelled while the batch was in flight — keep cancelled.
                with self._items_lock:
                    cancelled = item.id in self._cancelled
                    if cancelled:
                        self._cancelled.discard(item.id)
                if cancelled:
                    item.status = "cancelled"
                    item.msg = "已取消"
                    item.retry_count = 0
                    item.error_kind = ""
                    item.error_detail = ""
                    self._persist(item)
                    continue
                outcome = outcome_map.get(item.id)
                if (
                    not ok.get(item.id)
                    and outcome is not None
                    and outcome.error_kind == ErrorKind.CANCELLED
                ):
                    # P1-1: this is a *sibling* of the cancelled item, not the
                    # user's target. SteamCMD had not finished it, so queue it
                    # again instead of failing it. The bounded retry_count keeps
                    # a cancel-happy user from looping forever.
                    item.retry_count += 1
                    item.progress = 0
                    item.error_kind = ""
                    item.error_detail = ""
                    if item.retry_count > MAX_INTERRUPT_RETRIES:
                        item.status = "failed"
                        item.msg = "同批取消后重试仍未完成"
                        item.error_kind = ErrorKind.CANCELLED
                        item.error_detail = outcome.error_detail
                        self._persist(item)
                        self._record_history(item, "failed")
                    else:
                        item.status = "pending"
                        item.msg = "同批取消，已重新排队"
                        self._persist(item)
                    continue
                if ok.get(item.id):
                    final = _find_existing(config.mods_dir, item.id, instance_id=iid)
                    item.status = "done"
                    item.progress = 1.0
                    item.name = final.name if final else item.id
                    item.msg = "完成"
                    item.retry_count = 0
                    item.error_kind = ""
                    item.error_detail = ""
                    self._persist(item)
                    self._record_history(item, "success")
                else:
                    kind = (outcome.error_kind if outcome is not None else "") or ErrorKind.UNKNOWN
                    detail = (outcome.error_detail if outcome is not None else "") or ""
                    item.status = "failed"
                    item.progress = 0
                    item.msg = explain_outcome(kind)
                    item.error_kind = kind
                    item.error_detail = detail
                    item.retry_count = 0
                    self._persist(item)
                    self._record_history(item, "failed")
            await self._notify()

            # Re-queue collection children / missing dependencies — but only
            # once per drain episode. Without this, a collection that (directly
            # or transitively) contains itself makes the queue expand and
            # re-download the same mods forever.
            if extra:
                fresh_extra = [
                    eid for eid in dict.fromkeys(extra) if (iid, eid) not in self._expanded
                ]
                self._expanded.update((iid, eid) for eid in fresh_extra)
                if fresh_extra:
                    # Children/dependencies belong to the same instance as the
                    # collection that discovered them, not to whichever
                    # instance happens to be active by then.
                    self.add(fresh_extra, instance_id=iid)

    # ── persistence helpers ──────────────────────────────────────

    def _persist(self, item: QueueItem) -> None:
        """Write queue item state (and its scheduler flags) to SQLite."""
        try:
            from rwmod.database import queue_upsert

            queue_upsert(
                item.id,
                instance_id=item.instance_id or None,
                name=item.name,
                status=item.status,
                progress=item.progress,
                msg=item.msg,
                force=item.force,
                retry_count=item.retry_count,
            )
        except Exception:
            pass  # DB unavailable — gracefully degrade

    def _record_history(self, item: QueueItem, status: str) -> None:
        """Append a settled queue download to download_history (E2E-5).

        Only the /api/download routes used to record history, so downloads
        made through the queue (including one-click update) never appeared in
        /api/history or the dashboard activity stream.
        """
        try:
            from rwmod.database import record_download

            record_download(
                item.id,
                status,
                mod_name=item.name,
                msg=item.msg,
                instance_id=item.instance_id or None,
            )
        except Exception:
            pass  # history is best-effort, like _persist

    def _db_delete(self, workshop_id: str, instance_id: str | None = None) -> None:
        try:
            from rwmod.database import queue_delete

            queue_delete(workshop_id, instance_id=instance_id)
        except Exception:
            pass

    def _db_clear_done(self) -> None:
        try:
            from rwmod.database import queue_clear_done

            queue_clear_done()
        except Exception:
            pass

    def _load_from_db(self) -> None:
        """Load pending/downloading items from SQLite (used on startup).

        A persisted ``downloading`` row means the process died mid-download.
        Downloads are re-entrant, so it is requeued as ``pending`` — otherwise
        it is stranded forever: the worker only drains pending items and
        ``add()`` dedupes against downloading, so no later update run could
        ever reschedule it (DL-4).

        Each recovery increments ``retry_count``; after
        ``MAX_INTERRUPT_RETRIES`` consecutive interruptions the item fails
        instead of being requeued again, so a download that reliably kills the
        process cannot loop forever.
        """
        try:
            from rwmod.database import queue_load_pending

            rows = queue_load_pending()
            recovered: list[QueueItem] = []
            with self._items_lock:
                for row in rows:
                    status = str(row["status"])
                    retry_count = int(row.get("retry_count") or 0)
                    progress = row.get("progress", 0.0)
                    msg = row.get("msg", "")
                    changed = False
                    if status == "downloading":
                        changed = True
                        if retry_count >= MAX_INTERRUPT_RETRIES:
                            status = "failed"
                            progress = 0.0
                            msg = "下载中断，重试后仍未完成"
                        else:
                            status = "pending"
                            progress = 0.0
                            retry_count += 1
                            msg = "上次下载被中断，已重新排队"
                    item = QueueItem(
                        id=row["workshop_id"],
                        name=row.get("name", ""),
                        # Wave 6b: restart recovery keeps the owner so the
                        # resumed download targets the same installation.
                        instance_id=str(row.get("instance_id") or ""),
                        status=status,
                        progress=progress,
                        msg=msg,
                        force=bool(row.get("force") or False),
                        retry_count=retry_count,
                    )
                    self.items.append(item)
                    if changed:
                        recovered.append(item)
            for item in recovered:
                # Persist the recovery so a second death before the worker
                # picks the item up still observes the incremented retry count.
                self._persist(item)
        except Exception:
            pass  # DB unavailable, start empty


# Singleton
_queue: DownloadQueue | None = None


def get_queue() -> DownloadQueue:
    global _queue
    if _queue is None:
        _queue = DownloadQueue()
        # Restore pending items from previous session
        _queue._load_from_db()
    return _queue
