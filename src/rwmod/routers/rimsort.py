"""RimSort router."""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from rwmod.auth import get_current_user
from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.rimsort import (
    compare_modsconfig,
    generate_modsconfig,
    resolve_missing_workshop_ids,
    sort_mods,
)

router = APIRouter(prefix="/api/rimsort", tags=["rimsort"])


@router.post("/generate")
def rimsort_generate(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    return {"modsconfig_xml": generate_modsconfig(cfg.mods_dir)}


@router.post("/compare")
def rimsort_compare(
    payload: dict,
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    xml_content = payload.get("xml", "")
    if not xml_content.strip():
        raise HTTPException(400, "需要提供 ModsConfig.xml 内容")
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml_content)
        tmp = f.name
    try:
        result = compare_modsconfig(Path(tmp), cfg.mods_dir)
        if result.get("missing"):
            result["missing_details"] = resolve_missing_workshop_ids(
                result["missing"], cfg.mods_dir
            )
        return result
    finally:
        Path(tmp).unlink(missing_ok=True)


@router.post("/compare-file")
async def rimsort_compare_file(
    file: UploadFile = File(...),
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    content = await file.read()
    with tempfile.NamedTemporaryFile("wb", suffix=".xml", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        result = compare_modsconfig(Path(tmp), cfg.mods_dir)
        if result.get("missing"):
            result["missing_details"] = resolve_missing_workshop_ids(
                result["missing"], cfg.mods_dir
            )
        return result
    finally:
        Path(tmp).unlink(missing_ok=True)


@router.get("/check-order")
def check_load_order(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    from rwmod.load_order import check_load_order as _check
    from rwmod.profile import resolve_modsconfig_path

    path = resolve_modsconfig_path(cfg.rimworld_dir) or (cfg.rimworld_dir / "ModsConfig.xml")
    return _check(path, cfg.mods_dir)


@router.post("/sort")
def rimsort_sort(
    payload: dict | None = None,
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Preview the optimal load order (does not write anything)."""
    active_ids = (payload or {}).get("active_ids")
    return sort_mods(cfg.mods_dir, active_ids)


@router.post("/apply")
def rimsort_apply(
    cfg: Config = Depends(get_config),
    _user: str = Depends(get_current_user),
):
    """Sort the active ModsConfig.xml in place (backs up the original first)."""
    import xml.etree.ElementTree as ET

    from rwmod.profile import resolve_modsconfig_path

    path = resolve_modsconfig_path(cfg.rimworld_dir) or (cfg.rimworld_dir / "ModsConfig.xml")
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

    result = sort_mods(cfg.mods_dir, current)
    sorted_ids = result["sorted"] + result["unresolved"]

    # Preserve any active mods that sort_mods couldn't map (e.g. Core/DLC
    # without About.xml in mods_dir) by keeping them in their original order.
    known = set(sorted_ids)
    preserved = [pid for pid in current if pid not in known]
    final_order = sorted_ids + preserved

    # Backup original before overwriting
    import shutil
    from datetime import UTC, datetime

    backup = path.with_suffix(f".xml.bak_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, backup)

    # Snapshot for one-click undo
    from rwmod.undo import snapshot_modsconfig

    snapshot_modsconfig(path, label="sort")

    # Rewrite activeMods in the new order
    for li in active.findall("li"):
        active.remove(li)
    for pid in final_order:
        ET.SubElement(active, "li").text = pid
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)

    return {
        "ok": True,
        "msg": f"已应用排序（{len(final_order)} 个 Mod），原文件已备份为 {backup.name}",
        "backup": backup.name,
        "sorted": final_order,
        "cycles": result["cycles"],
    }
