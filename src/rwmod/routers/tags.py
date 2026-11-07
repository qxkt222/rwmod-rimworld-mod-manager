"""Tags router \u2014 API for managing mod tags/categories."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from rwmod.errors import ConflictError, ModNotFoundError
from rwmod.models.schemas import (
    FolderTagsResponse,
    OkResponse,
    TagFoldersResponse,
    TagListResponse,
    TagMutationResponse,
    TagRemoveAllResponse,
    TagRequest,
)
from rwmod.tags import (
    add_tag,
    get_mods_by_tag,
    get_tags,
    list_all_tags,
    remove_all_tags,
    remove_tag,
)

router = APIRouter(prefix="/api", tags=["tags"])


@router.get("/tags", response_model=TagListResponse)
def api_list_all_tags():
    """List all tags with mod counts."""
    return {"tags": list_all_tags()}


@router.get("/tags/by-tag/{tag}", response_model=TagFoldersResponse)
def api_get_mods_by_tag(tag: str):
    """List all mods with a given tag."""
    return {"tag": tag, "folders": get_mods_by_tag(tag)}


@router.get("/tags/{mod_folder}", response_model=FolderTagsResponse)
def api_get_mod_tags(mod_folder: str):
    """Get all tags for a specific mod folder."""
    return {"folder": mod_folder, "tags": get_tags(mod_folder)}


@router.post("/tags/{mod_folder}", response_model=TagMutationResponse)
def api_add_tag(
    mod_folder: str,
    payload: TagRequest,
):
    """Add a tag to a mod (duplicate → 409, not a silent 200 ``ok: false``)."""
    tag = payload.tag.strip()
    if not tag:
        raise HTTPException(400, "Tag is required")
    if not add_tag(mod_folder, tag):
        raise ConflictError(f"Tag 已存在: {tag}")
    return {"ok": True, "tag": tag, "folder": mod_folder}


@router.delete("/tags/{mod_folder}/{tag}", response_model=OkResponse)
def api_remove_tag(
    mod_folder: str,
    tag: str,
):
    """Remove a tag from a mod (missing → 404, not a silent 200 ``ok: false``)."""
    if not remove_tag(mod_folder, tag):
        raise ModNotFoundError(f"Tag 不存在: {tag}")
    return {"ok": True}


@router.delete("/tags/{mod_folder}", response_model=TagRemoveAllResponse)
def api_remove_all_tags(
    mod_folder: str,
):
    """Remove all tags from a mod."""
    count = remove_all_tags(mod_folder)
    return {"ok": True, "removed": count}
