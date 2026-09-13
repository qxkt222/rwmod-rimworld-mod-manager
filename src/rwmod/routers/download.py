"""Download + import routers."""

import asyncio
import contextlib
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from rwmod.config import Config
from rwmod.database import record_download
from rwmod.deps import get_config
from rwmod.downloader import (
    BATCH_SIZE,
    _find_existing,
    download_batch,
    download_one,
    extract_mod_id,
)
from rwmod.logger import get_log
from rwmod.parser import (
    get_installed_package_ids,
    parse_modlist_file,
    parse_mods_config,
    resolve_workshop_ids,
)
from rwmod.utils import read_upload_limited
from rwmod.workshop import fetch_collection_children, is_collection

router = APIRouter(prefix="/api", tags=["download"])
_log = get_log("rwmod.server")

# ── Upload limits ──────────────────────────────────────────────────────
# Modlists are plain text ID files (< 1MB); ModsConfig.xml is a few hundred KB.
# A hard cap prevents unbounded file uploads exhausting disk / memory.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

# Concurrent SteamCMD executions are CPU + disk heavy and Steam rate-limits
# parallel logins. Bound the thread pool so a batch download cannot flood the
# host with dozens of steamcmd processes at once.
MAX_CONCURRENT_DOWNLOADS = 2
_dl_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


async def _bounded_download(cfg: Config, mid: str, force: bool) -> bool:
    """Run download_one with a global concurrency cap and exception safety."""
    async with _dl_semaphore:
        try:
            return await asyncio.to_thread(download_one, cfg, mid, force=force)
        except Exception as e:  # noqa: BLE001 — never crash the batch / stream
            _log.error("下载 %s 异常: %s", mid, e, exc_info=True)
            return False


async def _bounded_download_stream(cfg: Config, mid: str, force: bool):
    """Run _bounded_download, yielding SSE heartbeat comments every 30s.

    A SteamCMD download can take up to 10 minutes with no output; without
    heartbeats an nginx-style proxy (default 60s read timeout) kills the
    stream mid-download and the UI reports failure while the download
    actually continues. Yields None for each heartbeat, then the final bool.
    """
    task = asyncio.create_task(_bounded_download(cfg, mid, force))
    try:
        while True:
            try:
                yield await asyncio.wait_for(asyncio.shield(task), timeout=30)
                return
            except TimeoutError:
                yield None  # heartbeat — keep the connection alive
    finally:
        if not task.done():
            task.cancel()


async def _drain_heartbeat(agen) -> bool:
    """Collect the final result from _bounded_download_stream (forwarding heartbeats)."""
    result: bool | None = None
    async for chunk in agen:
        if chunk is not None:
            result = chunk
    return bool(result)


def _build_existing_map(mods_dir: Path) -> dict[str, Path]:
    """Map workshop ID → mod folder in one pass (avoid per-item directory scans)."""
    result: dict[str, Path] = {}
    if not mods_dir.exists():
        return result
    for d in mods_dir.iterdir():
        if not d.is_dir():
            continue
        pf = d / "About" / "PublishedFileId.txt"
        try:
            if pf.exists():
                wid = pf.read_text(encoding="utf-8").strip()
                if wid.isdigit():
                    result[wid] = d
        except OSError:
            continue
    return result


def _reject_oversized(file: UploadFile) -> None:
    """Reject uploads larger than MAX_UPLOAD_BYTES without buffering the body.

    Note: UploadFile.size is None for chunked uploads, so callers must use
    read_upload_limited() for the actual byte enforcement — this pre-check
    only rejects the case where Content-Length is known.
    """
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024} KB）")


async def _read_modlist(file: UploadFile) -> str:
    """Read an uploaded modlist, enforcing MAX_UPLOAD_BYTES regardless of
    whether the client sent a Content-Length."""
    content = await read_upload_limited(file, MAX_UPLOAD_BYTES)
    if content is None:
        raise HTTPException(413, f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024} KB）")
    return content.decode("utf-8")


@router.post("/download")
async def download_mods(
    payload: dict,
    cfg: Config = Depends(get_config),
):
    ids: list[str] = payload.get("ids", [])
    force: bool = payload.get("force", False)
    parsed = [mid for raw in ids if (mid := extract_mod_id(raw))]
    if not parsed:
        raise HTTPException(400, "没有有效的 Mod ID")
    cfg.validate()
    results: list[dict] = []
    for mid in parsed:
        ok = await _bounded_download(cfg, mid, force)
        results.append({"id": mid, "ok": ok})
        status = "success" if ok else "failed"
        record_download(mid, status)
        _log.info("download %s → %s", mid, status)
    return {"total": len(results), "results": results}


@router.get("/download/stream")
async def download_stream(
    id: str,
    force: bool = False,
    cfg: Config = Depends(get_config),
):
    mid = extract_mod_id(id)
    if not mid:
        raise HTTPException(400, "无效的 Mod ID")
    cfg.validate()

    async def event_stream():
        try:
            async for chunk in _event_stream(cfg, mid, force):
                yield chunk
        except asyncio.CancelledError:
            # Client disconnected mid-stream: stop scheduling new downloads.
            _log.info("SSE 客户端断开，中止下载流 %s", mid)
            raise
        except Exception as e:  # noqa: BLE001
            _log.error("下载流 %s 异常: %s", mid, e, exc_info=True)
            with contextlib.suppress(Exception):
                yield f"data: {_sse_event('fail', msg='服务器内部错误')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _event_stream(cfg: Config, mid: str, force: bool):
    """SSE generator. Yields events for a single mod or a collection.

    All Web API / download work is pushed to a thread so a slow Steam API
    response never blocks the event loop (which would freeze every other
    request served by this FastAPI process).
    """
    yield f"data: {_sse_event('start', id=mid)}\n\n"
    if await asyncio.to_thread(is_collection, mid):
        yield f"data: {_sse_event('info', msg='检测到合集，通过 Web API 获取内容...')}\n\n"
        collection_ids = await asyncio.to_thread(fetch_collection_children, mid)
        if not collection_ids:
            yield f"data: {_sse_event('warn', msg='未能获取合集内容')}\n\n"
            yield f"data: {_sse_event('done', id=mid)}\n\n"
            return
        yield f"data: {_sse_event('info', msg=f'合集包含 {len(collection_ids)} 个 Mod，批量下载中...', total=len(collection_ids))}\n\n"
        # Build the installed map ONCE — per-item directory scans on a 500-mod
        # collection × 500 folders would stall the event loop.
        existing_map = await asyncio.to_thread(_build_existing_map, cfg.mods_dir)
        to_download: list[str] = []
        skip = 0
        for cid in collection_ids:
            existing = existing_map.get(cid)
            if existing and not force:
                yield f"data: {_sse_event('skip', msg=f'  {cid} — 已安装: {existing.name}')}\n\n"
                skip += 1
            else:
                to_download.append(cid)
        if skip:
            yield f"data: {_sse_event('info', msg=f'{skip} 个已安装，跳过；{len(to_download)} 个待下载', total=len(to_download))}\n\n"
        ok = 0
        fail = 0
        i = 0
        # Batch pipeline: BATCH_SIZE mods per SteamCMD process, with missing
        # dependencies / sub-collections (extra) automatically appended so the
        # whole dependency graph drains in one stream.
        pending = list(to_download)
        seen: set[str] = set(pending)
        while pending:
            batch = pending[:BATCH_SIZE]
            pending = pending[BATCH_SIZE:]
            yield f"data: {_sse_event('info', msg=f'[{i + 1}-{i + len(batch)}/{len(to_download)}] 批量下载中...')}\n\n"
            extra: list[str] = []
            # Live SteamCMD byte progress → forwarded as SSE 'progress'
            # events. download_batch runs in a worker thread, so the callback
            # (also on that thread) pushes into an asyncio.Queue via
            # call_soon_threadsafe; the generator drains it while awaiting
            # the batch, throttled to ~2 events/sec per mod.
            progress_queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            last_push = [0.0]

            def _progress(
                mod_id: str,
                percent: float,
                downloaded: float,
                total: float,
                _last_push: list[float] = last_push,
                _loop: asyncio.AbstractEventLoop = loop,
                _queue: asyncio.Queue = progress_queue,
            ) -> None:
                now = time.monotonic()
                if now - _last_push[0] < 0.5:
                    return
                _last_push[0] = now
                with contextlib.suppress(RuntimeError):
                    _loop.call_soon_threadsafe(
                        _queue.put_nowait,
                        {
                            "id": mod_id,
                            "percent": round(percent, 1),
                            "downloaded": int(downloaded),
                            "total": int(total),
                        },
                    )

            task = asyncio.create_task(
                asyncio.to_thread(
                    download_batch,
                    cfg,
                    batch,
                    force=force,
                    extra_out=extra,
                    progress_cb=_progress,
                )
            )
            # Drain progress events while the batch runs; a heartbeat info
            # event every 30s of silence keeps client-side timeouts (and
            # proxies) alive during SteamCMD's startup cache-validation,
            # which can take minutes with a large cache.
            last_heartbeat = [time.monotonic()]
            while not task.done():
                try:
                    evt = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                except TimeoutError:
                    if time.monotonic() - last_heartbeat[0] >= 30:
                        last_heartbeat[0] = time.monotonic()
                        yield f"data: {_sse_event('info', msg='仍在下载中（SteamCMD 校验/下载）...')}\n\n"
                    continue
                last_heartbeat[0] = time.monotonic()
                yield f"data: {_sse_event('progress', **evt)}\n\n"
            ok_map = task.result()
            for cid in batch:
                i += 1
                if ok_map.get(cid):
                    ok += 1
                    yield f"data: {_sse_event('ok', msg=f'  {cid}')}\n\n"
                else:
                    fail += 1
                    yield f"data: {_sse_event('warn', msg=f'  {cid} 失败')}\n\n"
            for eid in extra:
                if eid not in seen:
                    seen.add(eid)
                    pending.append(eid)
            if extra:
                yield f"data: {_sse_event('info', msg=f'发现 {len(extra)} 个新依赖/子合集，加入队列继续下载')}\n\n"
            await asyncio.sleep(0)
        yield f"data: {_sse_event('ok', msg=f'合集完成: {ok} 下载, {skip} 跳过, {fail} 失败')}\n\n"
        yield f"data: {_sse_event('done', id=mid)}\n\n"
        return

    existing = await asyncio.to_thread(_find_existing, cfg.mods_dir, mid)
    if existing and not force:
        yield f"data: {_sse_event('skip', msg=f'已安装: {existing.name}')}\n\n"
        yield f"data: {_sse_event('done', id=mid)}\n\n"
        return
    if existing and force:
        yield f"data: {_sse_event('info', msg=f'覆盖已有 mod: {existing.name}')}\n\n"
    if await _drain_heartbeat(_bounded_download_stream(cfg, mid, force)):
        final = await asyncio.to_thread(_find_existing, cfg.mods_dir, mid)
        name = final.name if final else mid
        yield f"data: {_sse_event('ok', msg=f'{name}', id=mid)}\n\n"
        record_download(mid, "success", mod_name=name)
    else:
        yield f"data: {_sse_event('fail', msg='下载失败（含 Skymods 备用源）', id=mid)}\n\n"
        record_download(mid, "failed")
    yield f"data: {_sse_event('done', id=mid)}\n\n"


@router.post("/import/file")
async def import_file(
    file: UploadFile = File(...),
    force: bool = False,
    cfg: Config = Depends(get_config),
):
    cfg.validate()
    _reject_oversized(file)
    content = await _read_modlist(file)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(content)
        tmp = f.name
    try:
        ids = parse_modlist_file(Path(tmp))
        results = []
        for mid in ids:
            ok = await _bounded_download(cfg, mid, force)
            results.append({"id": mid, "ok": ok})
        return {"total": len(results), "results": results}
    finally:
        Path(tmp).unlink(missing_ok=True)


@router.post("/import/collection")
async def import_collection_api(
    payload: dict,
    cfg: Config = Depends(get_config),
):
    raw_id = payload.get("collection_id", "")
    collection_id = extract_mod_id(raw_id)
    force: bool = payload.get("force", False)
    if not collection_id:
        raise HTTPException(400, f"无效的合集 ID: {raw_id}")
    cfg.validate()
    mod_ids = await asyncio.to_thread(fetch_collection_children, collection_id)
    if not mod_ids:
        raise HTTPException(404, "未能获取合集内容")
    _log.info("合集 %s 包含 %s 个 Mod", collection_id, len(mod_ids))
    # Batch pipeline: BATCH_SIZE mods per SteamCMD process; missing
    # dependencies / sub-collections (extra) are drained too.
    results: list[dict] = []
    pending = list(mod_ids)
    seen: set[str] = set(pending)
    while pending:
        batch = pending[:BATCH_SIZE]
        pending = pending[BATCH_SIZE:]
        extra: list[str] = []
        ok_map = await asyncio.to_thread(download_batch, cfg, batch, force=force, extra_out=extra)
        for mid in batch:
            results.append({"id": mid, "ok": ok_map.get(mid, False)})
        for eid in extra:
            if eid not in seen:
                seen.add(eid)
                pending.append(eid)
    return {"total": len(results), "results": results}


@router.post("/import/sort")
async def import_sort_api(
    file: UploadFile = File(...),
    force: bool = False,
    cfg: Config = Depends(get_config),
):
    cfg.validate()
    _reject_oversized(file)
    content = await read_upload_limited(file, MAX_UPLOAD_BYTES)
    if content is None:
        raise HTTPException(413, f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024} KB）")
    with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        package_ids = parse_mods_config(Path(tmp))
        installed = get_installed_package_ids(cfg.mods_dir)
        missing = [pid for pid in package_ids if pid not in installed]
        known, unknown = resolve_workshop_ids(missing, cfg.mods_dir)
        results = []
        for mid in known:
            ok = await _bounded_download(cfg, mid, force)
            results.append({"id": mid, "ok": ok})
        return {
            "total_packages": len(package_ids),
            "missing": len(missing),
            "unknown": unknown,
            "downloaded": len(results),
            "results": results,
        }
    finally:
        Path(tmp).unlink(missing_ok=True)


def _sse_event(event: str, **kwargs) -> str:
    import json

    return json.dumps({"event": event, **kwargs}, ensure_ascii=False)
