"""Queue router."""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config, get_queue
from rwmod.errors import ModNotFoundError
from rwmod.models.schemas import ModIdsRequest, QueueStartRequest, QueueStateResponse
from rwmod.queue import DownloadQueue
from rwmod.utils import extract_mod_id

router = APIRouter(prefix="/api", tags=["queue"])


@router.get("/queue", response_model=QueueStateResponse)
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
    instance_id: str | None = None,
    queue: DownloadQueue = Depends(get_queue),
):
    """Remove/cancel one queue item.

    ``instance_id`` (optional query parameter) disambiguates the same workshop
    id queued for two installations; cancel markers are scoped by it so
    cancelling one instance's download never aborts the other's.

    A missing item is a 404 (:class:`ModNotFoundError`). It used to answer
    200 ``{"ok": false}``, which let a failed delete look successful (REV-006).
    """
    if not queue.remove(mod_id, instance_id=instance_id):
        raise ModNotFoundError(f"队列中没有该任务: {mod_id}")
    return {"ok": True}


@router.post("/queue/clear")
def queue_clear(
    queue: DownloadQueue = Depends(get_queue),
):
    queue.clear_done()
    return {"ok": True}


@router.post("/queue/retry")
async def queue_retry(
    payload: ModIdsRequest | None = None,
    instance_id: str | None = None,
    cfg: Config = Depends(get_config),
    queue: DownloadQueue = Depends(get_queue),
):
    """Re-queue failed items (all when ``ids`` is empty).

    ``instance_id`` (optional query parameter) scopes the retry to one
    installation; it defaults to the currently active instance. The same
    workshop id may have failed for two installations, and retrying alpha's
    copy must not requeue beta's (REV-016).

    A no-op (nothing failed) is success: 200 with ``accepted: 0``.
    """
    ids = payload.ids if payload is not None else []
    accepted = queue.retry_failed(ids or None, instance_id=instance_id or cfg.active_instance)
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
