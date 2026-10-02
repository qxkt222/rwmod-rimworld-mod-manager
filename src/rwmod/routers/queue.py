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
    return {"items": queue.snapshot()}


@router.post("/queue/add")
def queue_add(
    payload: ModIdsRequest,
    queue: DownloadQueue = Depends(get_queue),
):
    ids: list[str] = payload.ids
    parsed = [mid for raw in ids if (mid := extract_mod_id(raw))]
    items = queue.add(parsed)
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
