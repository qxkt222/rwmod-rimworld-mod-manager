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

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── generic ────────────────────────────────────────────────────────


class OpenModel(BaseModel):
    """Base for response models that mirror a dict the backend already built.

    ``extra="allow"`` is deliberate: these models *document* the response so
    the frontend generator can type it, but they must never become a lossy
    transform that drops a field the router (or an older client) still sends.
    """

    model_config = ConfigDict(extra="allow")


class OkResponse(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    """The body of every failure, per the failure contract in CLAUDE.md.

    Was ``{detail: str}`` only, which no longer described any handler.
    """

    error: str
    detail: str


# ── config ─────────────────────────────────────────────────────────


class InstanceInfo(BaseModel):
    """One multi-instance entry in GET /api/config."""

    id: str
    name: str
    steamcmd_path: str
    mods_dir: str
    rimworld_dir: str
    backup_dir: str
    config_dir: str | None = None


class ConfigResponse(BaseModel):
    steamcmd_path: str
    mods_dir: str
    rimworld_dir: str
    backup_dir: str
    has_steam_api_key: bool
    steamcmd_exists: bool
    mods_dir_exists: bool
    instances: list[InstanceInfo] = Field(default_factory=list)
    active_instance: str = "default"


class InstanceCreateRequest(BaseModel):
    """POST /api/config/instances — create a second RimWorld installation.

    ``mods_dir`` is the only required path; ``rimworld_dir`` defaults to the
    parent directory and the remaining settings are inherited/defaulted.
    Field caps keep a single request from carrying unbounded strings.
    """

    id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=200)
    steamcmd_path: str | None = Field(default=None, max_length=4096)
    mods_dir: str = Field(max_length=4096)
    rimworld_dir: str | None = Field(default=None, max_length=4096)
    backup_dir: str | None = Field(default=None, max_length=4096)
    config_dir: str | None = Field(default=None, max_length=4096)


class ConfigUpdateRequest(BaseModel):
    """POST /api/config — only the keys actually sent are applied.

    ``None`` means "not provided"; the router reads ``model_fields_set`` rather
    than truthiness, so an empty string stays a deliberate value.
    """

    steamcmd_path: str | None = Field(default=None, max_length=4096)
    mods_dir: str | None = Field(default=None, max_length=4096)
    rimworld_dir: str | None = Field(default=None, max_length=4096)
    backup_dir: str | None = Field(default=None, max_length=4096)
    steam_api_key: str | None = Field(default=None, max_length=4096)


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
    """A batch of workshop ids. ``force`` is only consumed by /queue/add
    (update intent); the other consumers ignore it.

    The list cap (10k ids, each ≤512 chars) bounds a single request body well
    below the JSON middleware cap while allowing a full collection.
    """

    ids: list[str] = Field(default_factory=list, max_length=10_000)
    force: bool = False


class DownloadRequest(ModIdsRequest):
    force: bool = False


class CollectionImportRequest(BaseModel):
    collection_id: str = Field(default="", max_length=512)
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
    locale: str = Field(default="zh-CN", max_length=20)


class ProfileSaveRequest(BaseModel):
    name: str = Field(default="", max_length=200)


class TagRequest(BaseModel):
    tag: str = Field(default="", max_length=200)


class ModsConfigRequest(BaseModel):
    """Raw ModsConfig.xml text (compared or applied server-side).

    ``max_length`` is the per-field backstop; ``JsonBodyLimitMiddleware`` caps
    the whole request at 8 MiB (a 500-mod config is a few hundred KB).
    """

    xml: str = Field(default="", max_length=4_000_000)


class RimsortSortRequest(BaseModel):
    active_ids: list[str] | None = Field(default=None, max_length=10_000)


class BackupRestoreRequest(BaseModel):
    filename: str | None = Field(default=None, max_length=255)


class BackupCleanupRequest(BaseModel):
    keep: int = 5


class TransferExportRequest(BaseModel):
    include_backups: bool = True
    include_secrets: bool = False


# ── mods ───────────────────────────────────────────────────────────


class ModListItem(OpenModel):
    """One row of GET /api/mods."""

    folder: str
    name: str
    package_id: str
    workshop_id: str
    tags: list[str] = Field(default_factory=list)
    size_mb: float = 0.0
    size_pending: bool = False
    active: bool = False


# ── queue ──────────────────────────────────────────────────────────


class QueueItemState(OpenModel):
    id: str
    name: str = ""
    instance_id: str = ""
    status: str = ""
    progress: float = 0.0
    msg: str = ""
    downloaded: float = 0.0
    total: float = 0.0
    speed_bps: float = 0.0
    error_kind: str = ""
    error_detail: str = ""


class QueueStateResponse(OpenModel):
    items: list[QueueItemState] = Field(default_factory=list)
    paused: bool = False


# ── history ────────────────────────────────────────────────────────


class HistoryItem(OpenModel):
    id: int
    instance_id: str = ""
    workshop_id: str = ""
    mod_name: str = ""
    package_id: str = ""
    status: str = ""
    msg: str = ""
    created_at: str = ""


class HistoryResponse(OpenModel):
    items: list[HistoryItem] = Field(default_factory=list)


class DownloadStatsResponse(OpenModel):
    total: int = 0
    success: int = 0
    failed: int = 0


# ── backups ────────────────────────────────────────────────────────


class BackupItem(OpenModel):
    filename: str
    workshop_id: str
    folder_name: str
    timestamp: str
    size_mb: float


class BackupListResponse(OpenModel):
    backups: list[BackupItem] = Field(default_factory=list)
    backup_dir: str = ""


class BackupRestoreResponse(OpenModel):
    ok: bool = True
    msg: str = ""
    restored_folder: str | None = None


class BackupCleanupResponse(OpenModel):
    ok: bool = True
    deleted: int = 0


class BackupDeleteResponse(OpenModel):
    ok: bool = True


# ── profiles ───────────────────────────────────────────────────────


class ProfileItem(OpenModel):
    name: str
    mod_count: int = 0
    saved_at: str = ""
    size_kb: float = 0.0


class ProfileListResponse(OpenModel):
    profiles: list[ProfileItem] = Field(default_factory=list)
    modsconfig_path: str | None = None


class ProfileSaveResponse(OpenModel):
    ok: bool = True
    msg: str = ""
    mod_count: int = 0


# ── tags ───────────────────────────────────────────────────────────


class TagCount(OpenModel):
    tag: str
    count: int = 0


class TagListResponse(OpenModel):
    tags: list[TagCount] = Field(default_factory=list)


class TagFoldersResponse(OpenModel):
    tag: str
    folders: list[str] = Field(default_factory=list)


class FolderTagsResponse(OpenModel):
    folder: str
    tags: list[str] = Field(default_factory=list)


class TagMutationResponse(OpenModel):
    ok: bool = True
    tag: str = ""
    folder: str = ""


class TagRemoveAllResponse(OpenModel):
    ok: bool = True
    removed: int = 0


# ── rimsort ────────────────────────────────────────────────────────


class RimsortGenerateResponse(OpenModel):
    modsconfig_xml: str = ""


class MissingModDetail(OpenModel):
    package_id: str
    workshop_id: str = ""
    resolved: bool = False


class RimsortCompareResponse(OpenModel):
    load_order: list[str] = Field(default_factory=list)
    total_in_config: int = 0
    installed: list[str] = Field(default_factory=list)
    installed_count: int = 0
    missing: list[str] = Field(default_factory=list)
    missing_count: int = 0
    extra: list[str] = Field(default_factory=list)
    extra_count: int = 0
    missing_details: list[MissingModDetail] = Field(default_factory=list)


class SortViolation(OpenModel):
    kind: str = ""
    reason: str = ""


class RimsortSortResponse(OpenModel):
    sorted: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)
    violations: list[SortViolation] = Field(default_factory=list)
    rimworld_version: str = ""


class RimsortApplyResponse(OpenModel):
    ok: bool = True
    msg: str = ""
    backup: str = ""
    sorted: list[str] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)


class SteamDbStatusFields(OpenModel):
    cached: bool = False
    valid: bool = False
    entries: int = 0
    # The cache stores the Workshop DB's numeric version (a timestamp); older
    # caches may carry a string. Keep both so the response is never rejected.
    version: str | int | None = None
    updated_at: str | None = None
    size: int = 0
    stale: bool = False
    ttl_seconds: int = 0
    max_bytes: int = 0


class SteamDbStatusResponse(SteamDbStatusFields):
    # /status adds the config flag; /refresh does not.
    enabled: bool = False


class SteamDbRefreshResponse(SteamDbStatusFields):
    ok: bool = True


class LoadOrderIssue(OpenModel):
    severity: str
    message: str


class LoadOrderSteamDb(OpenModel):
    matched: int = 0
    game_versions: dict[str, list[str]] = Field(default_factory=dict)
    unsupported: list[dict[str, Any]] = Field(default_factory=list)


class LoadOrderResponse(OpenModel):
    total_mods: int = 0
    issues: list[LoadOrderIssue] = Field(default_factory=list)
    load_order: list[str] = Field(default_factory=list)
    ok: bool = False
    steam_db: LoadOrderSteamDb | None = None


# ── saves ──────────────────────────────────────────────────────────


class SaveSummary(OpenModel):
    name: str
    game_version: str | None = None
    total_mods: int = 0
    missing_count: int = 0
    loadable: bool = False
    completeness: float = 0.0


class SaveListResponse(OpenModel):
    saves: list[SaveSummary] = Field(default_factory=list)


class SaveDetailResponse(OpenModel):
    file: str
    name: str
    game_version: str | None = None
    total_mods: int = 0
    required_mods: list[str] = Field(default_factory=list)
    missing_mods: list[str] = Field(default_factory=list)
    unused_by_save: list[str] = Field(default_factory=list)
    completeness: float = 0.0
    loadable: bool = False


class SaveAnalyzeResponse(OpenModel):
    filename: str | None = None
    mods: list[str] = Field(default_factory=list)
    mod_count: int = 0


# ── dashboard ──────────────────────────────────────────────────────


class DashboardHealth(OpenModel):
    abandoned: int = 0
    stale: int = 0
    removed: int = 0
    stale_data: bool = False


class DashboardResponse(OpenModel):
    mods_count: int = 0
    updates_pending: int = 0
    disk_usage_mb: float = 0.0
    size_pending: bool = False
    recent_activity: list[HistoryItem] = Field(default_factory=list)
    health: DashboardHealth


# ── mod settings ───────────────────────────────────────────────────


class ModSettingFile(OpenModel):
    filename: str
    size: int = 0
    mtime: str = ""
    associated: bool = False
    workshop_id: str = ""
    package_id: str = ""
    name: str = ""


class ModSettingsListResponse(OpenModel):
    config_dir: str | None = None
    files: list[ModSettingFile] = Field(default_factory=list)


class ModSettingBackup(OpenModel):
    filename: str
    source: str = ""
    size: int = 0
    mtime: str = ""


class ModSettingBackupListResponse(OpenModel):
    backup_dir: str = ""
    backups: list[ModSettingBackup] = Field(default_factory=list)


class ModSettingBackupRow(OpenModel):
    filename: str
    backup: str = ""
    size: int = 0


class ModSettingBackupAllResponse(OpenModel):
    ok: bool = True
    count: int = 0
    backup_dir: str = ""
    backups: list[ModSettingBackupRow] = Field(default_factory=list)


class ModSettingContentResponse(OpenModel):
    filename: str
    size: int = 0
    mtime: str = ""
    xml: str = ""
    well_formed: bool = True
    root_tag: str = ""
    game_running: bool = False


class ModSettingWriteResponse(OpenModel):
    ok: bool = True
    filename: str
    size: int = 0
    backup: str = ""
    game_running: bool = False
    warning: str = ""


class ModSettingRestoreResponse(ModSettingWriteResponse):
    restored_from: str = ""
