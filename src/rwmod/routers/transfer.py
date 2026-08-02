"""Transfer router — one-click export/import of the full rwmod setup."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from rwmod.auth import get_current_user
from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.transfer import export_bundle, import_bundle

router = APIRouter(prefix="/api/transfer", tags=["transfer"])


@router.post("/export")
def api_export(
    payload: dict | None = None,
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Export profiles, tags, config & backups into a downloadable .rwmod file."""
    include_backups = (payload or {}).get("include_backups", True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out = Path(tempfile.gettempdir()) / f"rwmod_backup_{ts}.rwmod"
    result = export_bundle(cfg, out, include_backups=include_backups)
    if not result["ok"]:
        raise HTTPException(500, "导出失败")
    return FileResponse(
        out,
        media_type="application/zip",
        filename=out.name,
        headers={"X-Export-Summary": f"{result['profiles']} profiles, {result['backups']} backups"},
    )


@router.post("/import")
async def api_import(
    file: UploadFile = File(...),
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Import a .rwmod bundle, restoring profiles, tags, config & backups."""
    if not file.filename or not file.filename.lower().endswith(".rwmod"):
        raise HTTPException(400, "请上传 .rwmod 文件")
    content = await file.read()
    with tempfile.NamedTemporaryFile("wb", suffix=".rwmod", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        result = import_bundle(cfg, Path(tmp))
        if not result["ok"]:
            raise HTTPException(400, result.get("msg", "导入失败"))
        return result
    finally:
        Path(tmp).unlink(missing_ok=True)
