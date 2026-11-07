"""History router."""

from fastapi import APIRouter, Query

from rwmod.database import clear_history, get_download_history, get_download_stats
from rwmod.models.schemas import DownloadStatsResponse, HistoryResponse

router = APIRouter(prefix="/api", tags=["history"])


@router.get("/history", response_model=HistoryResponse)
def history(
    # Bounded: a huge int reaches sqlite3's INTEGER conversion and raises
    # OverflowError (HTTP 500). Negative values stay tolerated (SQLite treats
    # LIMIT -n as "no limit"), matching the existing contract.
    limit: int = Query(50, le=100_000),
    status: str = "",
):
    return {"items": get_download_history(limit=limit, status=status)}


@router.get("/history/stats", response_model=DownloadStatsResponse)
def history_stats():
    return get_download_stats()


@router.post("/history/clear")
def history_clear():
    clear_history()
    return {"ok": True}
