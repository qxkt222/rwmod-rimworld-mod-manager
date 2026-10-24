"""Queue router."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config, get_queue
from rwmod.models.schemas import ModIdsRequest, QueueStartRequest
from rwmod.queue import DownloadQueue
from rwmod.utils import extract_mod_id

router = APIRouter(prefix="/api", tags=["queue"])


@router.get("/queue")
def get_queue_state(
    queue: DownloadQueue = Depends(get_queue),
):
    return {"items": queue.snapshot(), "paused": queue.is_paused}


@router.post("/queue/add")
def queue_add(
    payload: ModIdsRequest,
    cfg: Config = Depends(get_config),
    queue: DownloadQueue = Depends(get_queue),
):
    """Queue ids. ``force=True`` marks them as updates (replace installed).

    The items are stamped with the *currently active* instance, so a later
    instance switch cannot retarget them at another mods directory.
    """
    ids: list[str] = payload.ids
    parsed = [mid for raw in ids if (mid := extract_mod_id(raw))]
    items = queue.add(parsed, force=payload.force, instance_id=cfg.active_instance)
    return {"added": len(items), "items": [{"id": i.id, "status": i.status} for i in items]}


@router.post("/queue/start")
async def queue_start(
    payload: QueueStartRequest | None = None,
    cfg: Config = Depends(get_config),
    queue: DownloadQueue = Depends(get_queue),
):
    force: bool = payload.force if payload else False
    cfg.validate()
    await queue.start(cfg, force=force)
    return {"ok": True}


@router.delete("/queue/{mod_id}")
def queue_remove(
    mod_id: str,
    queue: DownloadQueue = Depends(get_queue),
):
    return {"ok": queue.remove(mod_id)}


@router.post("/queue/clear")
def queue_clear(
    queue: DownloadQueue = Depends(get_queue),
):
    queue.clear_done()
    return {"ok": True}


@router.post("/queue/retry")
async def queue_retry(
    payload: ModIdsRequest | None = None,
    cfg: Config = Depends(get_config),
    queue: DownloadQueue = Depends(get_queue),
):
    """Re-queue failed items (all when ``ids`` is empty).

    A no-op (nothing failed) is success: 200 with ``accepted: 0``.
    """
    ids = payload.ids if payload is not None else []
    accepted = queue.retry_failed(ids or None)
    if accepted:
        # Make sure a worker exists to drain the re-queued items; start() is
        # idempotent and does not disturb a running worker.
        await queue.start(cfg)
    return {"accepted": accepted}


@router.post("/queue/pause")
def queue_pause(
    queue: DownloadQueue = Depends(get_queue),
):
    """Stop dispatching new batches; the in-flight batch finishes normally."""
    queue.pause()
    return {"paused": True}


@router.post("/queue/resume")
def queue_resume(
    queue: DownloadQueue = Depends(get_queue),
):
    queue.resume()
    return {"paused": False}
