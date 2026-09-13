"""Transfer router — one-click export/import of the full rwmod setup."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.transfer import export_bundle, import_bundle

router = APIRouter(prefix="/api/transfer", tags=["transfer"])

# .rwmod bundles can legitimately include large backups — 2GB still stops
# unbounded uploads.
_MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024  # 1 MB read/write chunk


def _save_upload(file: UploadFile, path: Path, max_bytes: int) -> None:
    """Stream an upload to disk with a hard byte cap.

    UploadFile.size is None for chunked uploads, so the size check cannot
    rely on Content-Length. Streaming to disk (instead of buffering the whole
    body in RAM) also keeps a multi-GB .rwmod import off the heap.
    """
    total = 0
    with path.open("wb") as fh:
        while True:
            chunk = file.file.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, f"文件过大（上限 {max_bytes // (1024 * 1024)} MB）")
            fh.write(chunk)


@router.post("/export")
def api_export(
    payload: dict | None = None,
    cfg: Config = Depends(get_config),
):
    """Export profiles, tags, config & backups into a downloadable .rwmod file.

    include_secrets=False by default: the bundle is meant to be shared, and
    the Steam API key is a credential. Opt in explicitly for machine-to-machine
    migration.
    """
    body = payload or {}
    include_backups = body.get("include_backups", True)
    include_secrets = bool(body.get("include_secrets", False))
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    # NamedTemporaryFile gives a unique name (two exports in the same second
    # can no longer overwrite each other); BackgroundTask deletes the file
    # once the response has been sent — no %TEMP% leak.
    with tempfile.NamedTemporaryFile("wb", suffix=".rwmod", delete=False) as f:
        out = Path(f.name)
    result = export_bundle(
        cfg, out, include_backups=include_backups, include_secrets=include_secrets
    )
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
):
    """Import a .rwmod bundle, restoring profiles, tags, config & backups."""
    if not file.filename or not file.filename.lower().endswith(".rwmod"):
        raise HTTPException(400, "请上传 .rwmod 文件")
    with tempfile.NamedTemporaryFile("wb", suffix=".rwmod", delete=False) as f:
        tmp = f.name
    try:
        # Stream to disk with a hard cap — chunked uploads have size=None,
        # and a multi-GB bundle must not be buffered in RAM.
        _save_upload(file, Path(tmp), _MAX_IMPORT_BYTES)
        result = import_bundle(cfg, Path(tmp))
        if not result["ok"]:
            raise HTTPException(400, result.get("msg", "导入失败"))
        return result
    finally:
        Path(tmp).unlink(missing_ok=True)
