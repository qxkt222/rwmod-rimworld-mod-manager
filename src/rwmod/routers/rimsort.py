"""RimSort router."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.errors import DownloadError, ValidationError
from rwmod.mod_rules import normalize_package_id
from rwmod.models.schemas import ModsConfigRequest, RimsortSortRequest
from rwmod.rimpy_db import refresh_community_rules
from rwmod.rimsort import (
    compare_modsconfig,
    generate_modsconfig,
    resolve_missing_workshop_ids,
    sort_mods,
)
from rwmod.steam_db import SteamDB
from rwmod.version_support import detect_rimworld_version

router = APIRouter(prefix="/api/rimsort", tags=["rimsort"])


@router.post("/generate")
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


@router.post("/compare")
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


@router.post("/compare-file")
async def rimsort_compare_file(
    file: UploadFile = File(...),
    cfg: Config = Depends(get_config),
):
    from rwmod.routers.download import _reject_oversized

    _reject_oversized(file)
    content = await file.read()
    with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        return _compare_or_raise(Path(tmp), cfg)
    finally:
        Path(tmp).unlink(missing_ok=True)


@router.get("/check-order")
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
        refresh_community_rules()
    path = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    return _check(
        path,
        cfg.mods_dir,
        rimworld_version=detect_rimworld_version(cfg.rimworld_dir),
        enable_steam_db=cfg.enable_steam_db,
    )


@router.post("/steamdb/refresh")
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


@router.get("/steamdb/status")
def steamdb_status(
    cfg: Config = Depends(get_config),
):
    """Report whether the Steam Workshop database is cached — never downloads."""
    status = SteamDB.get().status(max_bytes=cfg.steam_db_max_bytes)
    status["enabled"] = cfg.enable_steam_db
    return status


@router.post("/sort")
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
        refresh_community_rules()
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


@router.post("/apply")
def rimsort_apply(
    cfg: Config = Depends(get_config),
):
    """Sort the active ModsConfig.xml in place (backs up the original first)."""
    import xml.etree.ElementTree as ET

    from rwmod.profile import modsconfig_target

    # cfg.config_dir (when set) is authoritative; the target is sandbox-checked
    # before any backup/snapshot/write happens.
    path = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    if not path.exists():
        raise HTTPException(404, f"ModsConfig.xml 未找到: {path}")

    try:
        tree = ET.parse(path)
        root = tree.getroot()
        active = root.find("activeMods")
        if active is None:
            raise HTTPException(400, "ModsConfig.xml 中没有 activeMods")
        current = [li.text or "" for li in active.findall("li") if li.text]
    except ET.ParseError as e:
        raise HTTPException(400, f"解析 ModsConfig.xml 失败: {e}") from e

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

    # Backup original before overwriting
    import shutil
    from datetime import UTC, datetime

    backup = path.with_suffix(f".xml.bak_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, backup)

    # Snapshot for one-click undo
    from rwmod.undo import snapshot_modsconfig

    snapshot_modsconfig(path, label="sort")

    # Rewrite activeMods in the new order — atomically: write a temp file
    # first, then os.replace, so a crash mid-write can't leave a truncated
    # ModsConfig.xml.
    for li in active.findall("li"):
        active.remove(li)
    for pid in final_order:
        ET.SubElement(active, "li").text = pid
    ET.indent(tree, space="  ")
    tmp = path.with_suffix(path.suffix + ".rwmod.tmp")
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    import os

    os.replace(tmp, path)

    return {
        "ok": True,
        "msg": f"已应用排序（{len(final_order)} 个 Mod），原文件已备份为 {backup.name}",
        "backup": backup.name,
        "sorted": final_order,
        "cycles": result["cycles"],
    }
