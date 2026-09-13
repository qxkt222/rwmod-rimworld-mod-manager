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

from rwmod.config import Config
from rwmod.downloader import BATCH_SIZE, _find_existing, download_batch, download_one

__all__ = ["DownloadQueue", "get_queue", "MAX_CONCURRENT"]

_log = logging.getLogger(__name__)

# Concurrent SteamCMD *batches* (each batch = BATCH_SIZE mods in one process).
# Two processes already saturate most home connections; more would only
# contend for Steam's servers and the shared workshop_log.txt.
MAX_CONCURRENT = 2


@dataclass
class QueueItem:
    id: str
    name: str = ""
    status: str = "pending"  # pending | downloading | done | failed | cancelled
    progress: float = 0.0
    msg: str = ""
    # Live download telemetry (SteamCMD byte progress, updated by the reader
    # thread while a batch download is in flight).
    downloaded: float = 0.0  # bytes downloaded so far
    total: float = 0.0  # total bytes (0 while unknown)
    speed_bps: float = 0.0  # smoothed bytes/second


@dataclass
class DownloadQueue:
    items: list[QueueItem] = field(default_factory=list)
    _running: bool = False
    _semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT))
    _callbacks: list[Callable] = field(default_factory=list)
    # Workshop IDs whose in-flight download should be treated as cancelled.
    _cancelled: set[str] = field(default_factory=set)
    # Persistent background worker that drains pending items as they arrive.
    _worker: asyncio.Task | None = None
    _config: Config | None = None
    _force: bool = False
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Event loop captured in start(); used to wake the worker from sync
    # (FastAPI thread-pool) contexts via call_soon_threadsafe.
    _loop: asyncio.AbstractEventLoop | None = None
    # Guards items/_cancelled across the thread pool (sync /queue/* endpoints)
    # and the event-loop worker. Field-level mutations of QueueItem are safe
    # under the GIL; only list/set structure is protected.
    _items_lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, mod_ids: list[str]) -> list[QueueItem]:
        new_items: list[QueueItem] = []
        with self._items_lock:
            for mid in mod_ids:
                already = next(
                    (
                        i
                        for i in self.items
                        if i.id == mid and i.status in ("pending", "downloading")
                    ),
                    None,
                )
                if not already:
                    item = QueueItem(id=mid)
                    self.items.append(item)
                    new_items.append(item)
        for item in new_items:
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
        """
        cancelled_item: QueueItem | None = None
        removed_id: str | None = None
        with self._items_lock:
            for i, item in enumerate(self.items):
                if item.id == mod_id:
                    self._cancelled.add(mod_id)
                    if item.status == "downloading":
                        item.status = "cancelled"
                        item.msg = "已取消（正在终止 SteamCMD）"
                        cancelled_item = item
                    else:
                        self.items.pop(i)
                        removed_id = mod_id
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
        elif removed_id is not None:
            self._db_delete(removed_id)
        return True

    def clear_done(self) -> None:
        with self._items_lock:
            done_ids = [i.id for i in self.items if i.status in ("done", "cancelled")]
            self.items = [i for i in self.items if i.status not in ("done", "cancelled")]
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
                "status": i.status,
                "progress": i.progress,
                "msg": i.msg,
                "downloaded": i.downloaded,
                "total": i.total,
                "speed_bps": i.speed_bps,
            }
            for i in items
        ]

    async def start(self, config: Config, force: bool = False) -> None:
        """Start (or resume) draining the queue.

        Uses a single persistent worker task so that items added *while* the
        queue is already running are picked up too — the old implementation
        returned early when ``_running`` was True, silently dropping any
        pending items added after the first ``start()`` call.
        """
        async with self._lock:
            self._config = config
            self._force = force
            self._loop = asyncio.get_running_loop()
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._worker_loop())
            self._wake.set()

    def stop(self) -> None:
        """Cancel the persistent worker (called on server shutdown)."""
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()

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

    async def _worker_loop(self) -> None:
        """Drain pending items forever until the queue is empty and idle.

        Items are consumed in batches: up to BATCH_SIZE pending ids are
        handed to download_batch(), which downloads them in ONE SteamCMD
        process (one login) instead of starting a process per mod. Up to
        MAX_CONCURRENT batches run concurrently.
        """
        self._running = True
        try:
            while True:
                # Grab the next pending batch (if any).
                with self._items_lock:
                    batch = [i for i in self.items if i.status == "pending"][:BATCH_SIZE]
                if not batch:
                    # Nothing to do — wait for a wake-up (new item or start()).
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
                try:
                    await self._process_batch(config, batch, self._force)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — a crash must never kill the worker
                    _log.exception("队列 worker 处理批次异常: %s", [i.id for i in batch])
                    with self._items_lock:
                        for item in batch:
                            if item.status == "pending":
                                item.status = "failed"
                                item.progress = 0
                                item.msg = f"下载异常: {e}"
                    for item in batch:
                        self._persist(item)
                    await self._notify()
        finally:
            self._running = False
            await self._notify()

    async def _process_batch(self, config: Config, batch: list[QueueItem], force: bool) -> None:
        """Download one batch of items via a single SteamCMD process."""
        async with self._semaphore:
            ids = [i.id for i in batch]

            # Items cancelled while this batch waited for a slot stay cancelled.
            live: list[QueueItem] = []
            for item in batch:
                with self._items_lock:
                    cancelled = item.id in self._cancelled
                    if cancelled:
                        self._cancelled.discard(item.id)
                if cancelled:
                    item.status = "cancelled"
                    item.msg = "已取消"
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
                existing = _find_existing(config.mods_dir, item.id)
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

            for item in live:
                # Cancelled while the batch was in flight — keep cancelled.
                with self._items_lock:
                    cancelled = item.id in self._cancelled
                    if cancelled:
                        self._cancelled.discard(item.id)
                if cancelled:
                    item.status = "cancelled"
                    item.msg = "已取消"
                    self._persist(item)
                    continue
                if ok.get(item.id):
                    final = _find_existing(config.mods_dir, item.id)
                    item.status = "done"
                    item.progress = 1.0
                    item.name = final.name if final else item.id
                    item.msg = "完成"
                else:
                    item.status = "failed"
                    item.progress = 0
                    item.msg = "下载失败（含 Skymods 备用源）"
                self._persist(item)
            await self._notify()

            # Re-queue collection children / missing dependencies.
            if extra:
                self.add(extra)

    async def _download_one(self, config: Config, item: QueueItem, force: bool) -> None:
        async with self._semaphore:
            # Item was removed (cancelled) while this task waited for a slot
            with self._items_lock:
                cancelled = item.id in self._cancelled
            if cancelled:
                with self._items_lock:
                    self._cancelled.discard(item.id)
                return

            item.status = "downloading"
            item.progress = 0.1
            item.msg = "检查中..."
            self._persist(item)
            await self._notify()

            # Check already installed
            existing = _find_existing(config.mods_dir, item.id)
            if existing and not force:
                item.status = "done"
                item.progress = 1.0
                item.name = existing.name
                item.msg = "已安装"
                self._persist(item)
                await self._notify()
                return

            if existing and force:
                item.name = existing.name
                item.msg = "覆盖中..."
                self._persist(item)
                await self._notify()

            # Delegate to the unified download_one (blocking — runs in thread)
            item.msg = "下载中..."
            self._persist(item)
            await self._notify()

            ok = await asyncio.to_thread(download_one, config, item.id, force=force)

            # Cancelled while the download was in flight — keep cancelled state
            with self._items_lock:
                cancelled = item.id in self._cancelled
                if cancelled:
                    self._cancelled.discard(item.id)
            if cancelled:
                item.status = "cancelled"
                item.msg = "已取消"
                self._persist(item)
                await self._notify()
                return

            if ok:
                final = _find_existing(config.mods_dir, item.id)
                item.status = "done"
                item.progress = 1.0
                item.name = final.name if final else item.id
                item.msg = "完成"
            else:
                item.status = "failed"
                item.progress = 0
                item.msg = "下载失败（含 Skymods 备用源）"

            self._persist(item)
            await self._notify()

    # ── persistence helpers ──────────────────────────────────────

    def _persist(self, item: QueueItem) -> None:
        """Write queue item state to SQLite."""
        try:
            from rwmod.database import queue_upsert

            queue_upsert(
                item.id,
                name=item.name,
                status=item.status,
                progress=item.progress,
                msg=item.msg,
            )
        except Exception:
            pass  # DB unavailable — gracefully degrade

    def _db_delete(self, workshop_id: str) -> None:
        try:
            from rwmod.database import queue_delete

            queue_delete(workshop_id)
        except Exception:
            pass

    def _db_clear_done(self) -> None:
        try:
            from rwmod.database import queue_clear_done

            queue_clear_done()
        except Exception:
            pass

    def _load_from_db(self) -> None:
        """Load pending/downloading items from SQLite (used on startup)."""
        try:
            from rwmod.database import queue_load_pending

            rows = queue_load_pending()
            with self._items_lock:
                for row in rows:
                    item = QueueItem(
                        id=row["workshop_id"],
                        name=row.get("name", ""),
                        status=row["status"],
                        progress=row.get("progress", 0.0),
                        msg=row.get("msg", ""),
                    )
                    self.items.append(item)
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
