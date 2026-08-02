"""History router."""

from fastapi import APIRouter, Depends

from rwmod.auth import get_current_user
from rwmod.database import clear_history, get_download_history, get_download_stats

router = APIRouter(prefix="/api", tags=["history"])


@router.get("/history")
def history(
    limit: int = 50,
    status: str = "",
    _user: str = Depends(get_current_user),
):
    return {"items": get_download_history(limit=limit, status=status)}


@router.get("/history/stats")
def history_stats(_user: str = Depends(get_current_user)):
    return get_download_stats()


@router.post("/history/clear")
def history_clear(_user: str = Depends(get_current_user)):
    clear_history()
    return {"ok": True}
