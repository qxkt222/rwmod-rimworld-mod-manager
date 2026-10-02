"""Pydantic request/response models — shared across all routers.

All schemas live here. models/__init__.py re-exports for backward compat.

Request models are deliberately *permissive*: every field defaults to the same
value the router used to reach for via ``payload.get(key, default)``. That keeps
each semantic check — and the 400 it raises — exactly where it was, while a wrong
*type*, which used to fall through as silently-ignored garbage, now fails loudly
with 422. Tightening these models would move status codes, so don't do it
casually: a field made required turns an existing 400 into a 422.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── generic ────────────────────────────────────────────────────────


class OkResponse(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    """The body of every failure, per the failure contract in CLAUDE.md.

    Was ``{detail: str}`` only, which no longer described any handler.
    """

    error: str
    detail: str


# ── config ─────────────────────────────────────────────────────────


class ConfigResponse(BaseModel):
    steamcmd_path: str
    mods_dir: str
    rimworld_dir: str
    backup_dir: str
    has_steam_api_key: bool
    steamcmd_exists: bool
    mods_dir_exists: bool


class ConfigUpdateRequest(BaseModel):
    """POST /api/config — only the keys actually sent are applied.

    ``None`` means "not provided"; the router reads ``model_fields_set`` rather
    than truthiness, so an empty string stays a deliberate value.
    """

    steamcmd_path: str | None = None
    mods_dir: str | None = None
    rimworld_dir: str | None = None
    backup_dir: str | None = None
    steam_api_key: str | None = None


# ── search ─────────────────────────────────────────────────────────


class ModSearchItem(BaseModel):
    """One workshop search hit.

    Field-for-field what the search panel renders. It used to be emitted as
    ``ModSearchResult.__dict__``, which meant any field added to that dataclass
    for internal reasons silently became part of the public response.
    """

    id: str
    title: str
    author: str
    description: str
    preview_url: str
    rating: str
    subscribers: str
    installed: bool


class SearchResponse(BaseModel):
    results: list[ModSearchItem]


# ── download ───────────────────────────────────────────────────────


class ModIdsRequest(BaseModel):
    """A batch of workshop ids, nothing else."""

    ids: list[str] = Field(default_factory=list)


class DownloadRequest(ModIdsRequest):
    force: bool = False


class CollectionImportRequest(BaseModel):
    collection_id: str = ""
    force: bool = False


class QueueStartRequest(BaseModel):
    force: bool = False


class DownloadResultItem(BaseModel):
    id: str
    ok: bool


class FoldersRequest(BaseModel):
    """A batch of mod folder names (delete / re-scan style endpoints)."""

    folders: list[str] = Field(default_factory=list)


# ── misc requests ──────────────────────────────────────────────────


class LocaleRequest(BaseModel):
    locale: str = "zh-CN"


class ProfileSaveRequest(BaseModel):
    name: str = ""


class TagRequest(BaseModel):
    tag: str = ""


class ModsConfigRequest(BaseModel):
    """Raw ModsConfig.xml text (compared or applied server-side)."""

    xml: str = ""


class RimsortSortRequest(BaseModel):
    active_ids: list[str] | None = None


class BackupRestoreRequest(BaseModel):
    filename: str | None = None


class BackupCleanupRequest(BaseModel):
    keep: int = 5


class TransferExportRequest(BaseModel):
    include_backups: bool = True
    include_secrets: bool = False
