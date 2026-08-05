"""Transfer router — one-click export/import of the full rwmod setup."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from rwmod.auth import get_current_user
from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.transfer import export_bundle, import_bundle

router = APIRouter(prefix="/api/transfer", tags=["transfer"])

# .rwmod bundles can legitimately include large backups — 2GB still stops
# unbounded uploads.
_MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024


@router.post("/export")
def api_export(
    payload: dict | None = None,
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Export profiles, tags, config & backups into a downloadable .rwmod file."""
    include_backups = (payload or {}).get("include_backups", True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    # NamedTemporaryFile gives a unique name (two exports in the same second
    # can no longer overwrite each other); BackgroundTask deletes the file
    # once the response has been sent — no %TEMP% leak.
    with tempfile.NamedTemporaryFile("wb", suffix=".rwmod", delete=False) as f:
        out = Path(f.name)
    result = export_bundle(cfg, out, include_backups=include_backups)
    if not result["ok"]:
        out.unlink(missing_ok=True)
        raise HTTPException(500, "导出失败")
    return FileResponse(
        out,
        media_type="application/zip",
        filename=f"rwmod_backup_{ts}.rwmod",
        headers={"X-Export-Summary": f"{result['profiles']} profiles, {result['backups']} backups"},
        background=BackgroundTask(out.unlink, missing_ok=True),
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
    if file.size is not None and file.size > _MAX_IMPORT_BYTES:
        raise HTTPException(413, f"文件过大（上限 {_MAX_IMPORT_BYTES // (1024 * 1024)} MB）")
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
