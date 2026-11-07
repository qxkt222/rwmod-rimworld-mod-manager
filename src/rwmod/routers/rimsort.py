"""RimSort router."""

import re
import tempfile
from pathlib import Path
from typing import cast
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.errors import DownloadError, ModNotFoundError, ValidationError
from rwmod.mod_rules import normalize_package_id
from rwmod.models.schemas import (
    LoadOrderResponse,
    ModsConfigRequest,
    RimsortApplyResponse,
    RimsortCompareResponse,
    RimsortGenerateResponse,
    RimsortSortRequest,
    RimsortSortResponse,
    SteamDbRefreshResponse,
    SteamDbStatusResponse,
)
from rwmod.rimpy_db import refresh_community_rules
from rwmod.rimsort import (
    compare_modsconfig,
    generate_modsconfig,
    resolve_missing_workshop_ids,
    sort_mods,
)
from rwmod.steam_db import SteamDB
from rwmod.version_support import detect_rimworld_version
from rwmod.xmlutil import parse_xml_root

router = APIRouter(prefix="/api/rimsort", tags=["rimsort"])


def _refresh_rules_or_raise() -> None:
    """Force a Community Rules refresh; a failed one is a visible error.

    ``refresh_community_rules()`` reports success/failure (the old silent
    no-op hid a 24h cache miss). The stale cache is still used to compute the
    result — the refresh is fail-open — but because the caller explicitly
    asked for ``refresh_rules=true``, answering 200 would misreport a network
    failure as "rules are up to date". ``DownloadError`` is the errors.py
    exception for a failed download (502).
    """
    if refresh_community_rules():
        return
    from rwmod.rimpy_db import RimPyDB

    reason = RimPyDB.get().last_refresh_error or "未知原因"
    raise DownloadError(f"Community Rules 刷新失败: {reason}")


@router.post("/generate", response_model=RimsortGenerateResponse)
def rimsort_generate(
    cfg: Config = Depends(get_config),
):
    return {"modsconfig_xml": generate_modsconfig(cfg.mods_dir, rimworld_dir=cfg.rimworld_dir)}


def _compare_or_raise(path: Path, cfg: Config) -> dict:
    """Run the comparison, turning a parse failure into a 4xx.

    ``compare_modsconfig`` reports a parse failure as ``{"error": ...}`` for
    library callers, but a 200 body must carry data only (CLAUDE.md failure
    contract). The router therefore converts it to ``ValidationError`` so the
    status code carries the failure and the body is ``{error, detail}``.
    """
    result = compare_modsconfig(path, cfg.mods_dir)
    if "error" in result:
        raise ValidationError(f"解析 ModsConfig.xml 失败: {result['error']}")
    if result.get("missing"):
        result["missing_details"] = resolve_missing_workshop_ids(result["missing"], cfg.mods_dir)
    return result


@router.post("/compare", response_model=RimsortCompareResponse)
def rimsort_compare(
    payload: ModsConfigRequest,
    cfg: Config = Depends(get_config),
):
    xml_content = payload.xml
    if not xml_content.strip():
        raise HTTPException(400, "需要提供 ModsConfig.xml 内容")
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml_content)
        tmp = f.name
    try:
        return _compare_or_raise(Path(tmp), cfg)
    finally:
        Path(tmp).unlink(missing_ok=True)


@router.post("/compare-file", response_model=RimsortCompareResponse)
async def rimsort_compare_file(
    file: UploadFile = File(...),
    cfg: Config = Depends(get_config),
):
    from rwmod.routers.download import MAX_UPLOAD_BYTES, _reject_oversized
    from rwmod.utils import read_upload_limited

    _reject_oversized(file)
    # Chunked uploads carry no Content-Length; read with a hard cap instead of
    # `await file.read()` (which buffered any size into memory).
    content = await read_upload_limited(file, MAX_UPLOAD_BYTES)
    if content is None:
        raise HTTPException(413, f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024} KB）")
    with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        return _compare_or_raise(Path(tmp), cfg)
    finally:
        Path(tmp).unlink(missing_ok=True)


@router.get("/check-order", response_model=LoadOrderResponse, response_model_exclude_none=True)
def check_load_order(
    refresh_rules: bool = False,
    cfg: Config = Depends(get_config),
):
    """Analyze the active load order.

    ``refresh_rules=true`` forces a Community Rules download before analysis;
    the default path is cache-only so the endpoint stays offline-capable.

    Raises ModNotFoundError (404) / ValidationError (400) when ModsConfig.xml is
    absent or unreadable — load_order raises rather than returning an `error`
    key, so this endpoint no longer answers 200 with error text in the body.
    """
    from rwmod.load_order import check_load_order as _check
    from rwmod.profile import modsconfig_target

    if refresh_rules:
        _refresh_rules_or_raise()
    path = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    return _check(
        path,
        cfg.mods_dir,
        rimworld_version=detect_rimworld_version(cfg.rimworld_dir),
        enable_steam_db=cfg.enable_steam_db,
    )


@router.post("/steamdb/refresh", response_model=SteamDbRefreshResponse)
def steamdb_refresh(
    cfg: Config = Depends(get_config),
):
    """Explicitly download the Steam Workshop database (opt-in, ~47 MB).

    This POST *is* the explicit trigger — no other code path downloads the
    database, and ``GET /status`` never does. A failed download keeps the
    previous cache and is reported through the ``errors.py`` failure contract
    (502) rather than a 200 body with an ``ok: false`` flag.
    """
    db = SteamDB.get()
    if not db.refresh(max_bytes=cfg.steam_db_max_bytes):
        raise DownloadError("Steam Workshop 数据库下载失败（网络或数据格式错误）")
    return {"ok": True, **db.status(max_bytes=cfg.steam_db_max_bytes)}


@router.get("/steamdb/status", response_model=SteamDbStatusResponse)
def steamdb_status(
    cfg: Config = Depends(get_config),
):
    """Report whether the Steam Workshop database is cached — never downloads."""
    status = SteamDB.get().status(max_bytes=cfg.steam_db_max_bytes)
    status["enabled"] = cfg.enable_steam_db
    return status


@router.post("/sort", response_model=RimsortSortResponse)
def rimsort_sort(
    payload: RimsortSortRequest | None = None,
    refresh_rules: bool = False,
    cfg: Config = Depends(get_config),
):
    """Preview the optimal load order (does not write anything).

    Unknown ids inside an explicit ``active_ids`` list (Core/DLC are the usual
    case) are reported as ``unresolved`` rather than rejected. A list in which
    *no* id maps to an installed mod is a client error: every non-empty
    sortable list yields either sorted ids or a cycle report, so an empty
    result with no cycles means nothing was installed to sort.

    Community rules come from the on-disk cache; ``refresh_rules=true`` opts
    into a network refresh first.
    """
    if refresh_rules:
        _refresh_rules_or_raise()
    active_ids = payload.active_ids if payload else None
    result = sort_mods(
        cfg.mods_dir,
        active_ids,
        rimworld_version=detect_rimworld_version(cfg.rimworld_dir),
    )
    if active_ids and not result["sorted"] and not result["cycles"]:
        listed = ", ".join(active_ids[:5])
        raise ValidationError(f"active_ids 中没有已安装的 Mod，无法排序: {listed}")
    return result


#: Matches the inner text of ``<activeMods>…</activeMods>``. Only this one
#: block is rewritten; the rest of the file is preserved byte-for-byte.
_ACTIVE_BLOCK_RE = re.compile(r"(<activeMods(?:\s[^>]*)?>)(.*?)(</activeMods\s*>)", re.S)
_UTF8_BOM = b"\xef\xbb\xbf"


def _reject_cross_site(request: Request) -> None:
    """Refuse a state-changing request made from another web origin.

    ``/apply`` rewrites the live ModsConfig.xml and used to accept *any* POST.
    A cross-site form/``text/plain`` POST is a CORS-"simple" request — the
    browser sends it without a preflight, so no CORS policy protects it. The
    browser does attach ``Origin`` to every cross-site request (including
    simple ones), so rejecting a non-local, non-same-origin Origin closes the
    CSRF hole without requiring a token. Non-browser callers send no Origin and
    are unaffected.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    from rwmod.server import _origin_allowed

    if not _origin_allowed(origin, request.headers.get("host", "")):
        raise HTTPException(403, f"拒绝跨站请求: {origin}")


def _rewrite_active_block(raw: bytes, ids: list[str]) -> bytes:
    """Replace the ``<activeMods>`` list, preserving the file's formatting.

    The old implementation parsed the XML into a tree and wrote the whole
    document back, which dropped every comment, the UTF-8 BOM, the CRLF line
    endings and the original indentation. RimWorld tolerates all of that, but
    the user's hand-annotated file did not survive a sort. Here only the inner
    list is regenerated: the XML declaration, comments, ``knownExpansions`` and
    every other byte stay exactly as they were.
    """
    has_bom = raw.startswith(_UTF8_BOM)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValidationError(f"ModsConfig.xml 不是 UTF-8 编码: {e}") from e

    match = _ACTIVE_BLOCK_RE.search(text)
    if match is None:
        raise ValidationError("ModsConfig.xml 中没有 activeMods")

    inner = match.group(2)
    newline = "\r\n" if "\r\n" in inner else "\n"
    indent_match = re.search(r"(?:^|\r?\n)([ \t]*)<li\b", inner)
    indent = indent_match.group(1) if indent_match else "  "
    close_match = re.search(r"([ \t]*)$", inner)
    close_indent = close_match.group(1) if close_match else ""

    body = "".join(f"{newline}{indent}<li>{escape(pid)}</li>" for pid in ids)
    new_inner = body + newline + close_indent

    new_text = text[: match.start(2)] + new_inner + text[match.end(2) :]
    data = new_text.encode("utf-8")
    return (_UTF8_BOM + data) if has_bom else data


@router.post("/apply", response_model=RimsortApplyResponse)
def rimsort_apply(
    # Annotated as ``Request`` (not ``Request | None``) so FastAPI treats it as
    # the request object rather than a body field; the ``cast`` default keeps
    # the function directly callable — tests/CLI helpers call
    # ``rimsort_apply(cfg=...)`` with no HTTP request, and then there is no
    # Origin header to check.
    request: Request = cast(Request, None),
    cfg: Config = Depends(get_config),
):
    """Sort the active ModsConfig.xml in place (backs up the original first).

    Preserves the original file's BOM, comments, line endings and indentation —
    only the ``<activeMods>`` list is rewritten.
    """
    from rwmod.modsconfig_edit import write_modsconfig_target
    from rwmod.profile import modsconfig_target

    if request is not None:
        _reject_cross_site(request)

    # cfg.config_dir (when set) is authoritative; the target is sandbox-checked
    # before any backup/snapshot/write happens.
    path = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    if not path.exists():
        raise ModNotFoundError(f"ModsConfig.xml 未找到: {path}")

    try:
        # defused parser: this file can be edited by hand/hostile tooling.
        root = parse_xml_root(path)
    except Exception as e:
        raise ValidationError(f"解析 ModsConfig.xml 失败: {e}") from e
    active = root.find("activeMods")
    if active is None:
        raise ValidationError("ModsConfig.xml 中没有 activeMods")
    current = [li.text or "" for li in active.findall("li") if li.text]

    result = sort_mods(
        cfg.mods_dir,
        current,
        rimworld_version=detect_rimworld_version(cfg.rimworld_dir),
    )
    # sort_mods anchors Harmony/Core/DLC, returns every resolvable id in
    # `sorted`, and puts everything it could not place in `unresolved` (in the
    # caller's order). Nothing is dropped silently.
    final_order = result["sorted"] + result["unresolved"]

    # Safety net: if sort_mods ever fails to return an active id, keep it in
    # its original position at the end rather than losing the mod.
    placed = {normalize_package_id(pid) for pid in final_order if pid}
    for pid in current:
        norm = normalize_package_id(pid)
        if norm and norm not in placed:
            final_order.append(pid)
            placed.add(norm)

    # Rewrite the list before touching anything on disk, so a malformed file
    # fails while the live ModsConfig is still untouched.
    new_data = _rewrite_active_block(path.read_bytes(), final_order)

    # Backup + undo snapshot + atomic write live in one helper. A fixed
    # "<target>.rwmod.tmp" let two concurrent applies share one scratch file —
    # one writer's os.replace moved the other's half-written content into
    # place (REV-010). The helper uses tempfile.mkstemp (unique per writer) and
    # keeps the microsecond-stamped backup with a collision suffix, so two
    # applies inside the same wall-clock second cannot destroy the pre-sort
    # backup (0.7.0 incident).
    # Pass the bytes through untouched: the writer accepts bytes verbatim, so
    # a BOM-prefixed file never round-trips through a Python str.
    backup = write_modsconfig_target(path, new_data, label="sort")
    backup_name = backup.name if backup is not None else ""

    return {
        "ok": True,
        "msg": f"已应用排序（{len(final_order)} 个 Mod），原文件已备份为 {backup_name}",
        "backup": backup_name,
        "sorted": final_order,
        "cycles": result["cycles"],
    }
