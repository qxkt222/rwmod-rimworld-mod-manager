"""Persistent mod metadata cache backed by SQLite.

Provides per-mod incremental refresh: on startup, only re-parses About.xml
for mod folders whose mtime has changed since the last cache write.
New mods are scanned; deleted mods are cleaned up automatically.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from rwmod.config import current_instance_id, resolve_instance_id
from rwmod.database import get_conn
from rwmod.metadata import ModMeta, read_mod_metadata


def _decode_supported_versions(raw: str | None) -> list[str] | None:
    """Decode the cached JSON array. ``None`` means "not cached yet" (a row
    written before schema v2, or a corrupt value) and forces a re-parse."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list):
        return None
    return [str(v) for v in value]


def get_or_refresh_metas(mods_dir: Path, instance_id: str | None = None) -> list[ModMeta]:
    """Return mod metadata using SQLite cache with per-mod incremental refresh.

    The cache is scoped to an instance: two instances may each own a folder
    with the same name and different About.xml. ``instance_id`` defaults to
    the instance owning *mods_dir* (or the active one).

    Strategy:
    1. Read all cached entries for the instance from local_mod_metadata.
    2. Scan mods_dir via os.scandir (fast — DirEntry caches stat).
    3. For each mod folder:
       - Cache hit + mtime unchanged → use cached data (zero I/O).
       - Cache miss or mtime changed → parse About.xml.
    4. Remove deleted mods from cache.
    5. Write back newly parsed entries.

    Returns:
        List of ModMeta, sorted by folder name for deterministic output.
    """
    iid = instance_id or resolve_instance_id(mods_dir)
    if not mods_dir.exists():
        _clear_all(iid)
        return []

    db = get_conn()
    cached = {
        row["folder"]: dict(row)
        for row in db.execute(
            "SELECT * FROM local_mod_metadata WHERE instance_id = ?", (iid,)
        ).fetchall()
    }

    current_folders: set[str] = set()
    metas: list[ModMeta] = []
    needs_upsert: list[ModMeta] = []
    deleted: list[str] = []

    with os.scandir(mods_dir) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            if not entry.is_dir():
                continue
            folder = entry.name
            current_folders.add(folder)

            row = cached.get(folder)
            if row is not None and row["dir_mtime"] >= entry.stat().st_mtime:
                # Cache hit — folder unchanged since last scan. Rows written by
                # schema v1 have NULL supported_versions; treat those as a miss
                # so the field is backfilled instead of reporting every mod as
                # "unknown" on warm reads.
                supported = _decode_supported_versions(row.get("supported_versions"))
                if supported is not None:
                    metas.append(
                        ModMeta(
                            folder=folder,
                            name=row["name"],
                            package_id=row["package_id"],
                            workshop_id=row["workshop_id"],
                            supported_versions=supported,
                        )
                    )
                    continue
                # Fall through to re-parse and backfill.
            meta = read_mod_metadata(Path(entry.path))
            if meta:
                metas.append(meta)
                needs_upsert.append(meta)

    # Remove deleted mods from cache
    deleted = [f for f in cached if f not in current_folders]

    # Write changes in a single transaction
    if deleted or needs_upsert:
        now = time.time()
        with db:
            for folder in deleted:
                db.execute(
                    "DELETE FROM local_mod_metadata WHERE instance_id = ? AND folder = ?",
                    (iid, folder),
                )
            for m in needs_upsert:
                mod_path = mods_dir / m.folder
                mtime = mod_path.stat().st_mtime if mod_path.is_dir() else 0
                # Explicit column list + ON CONFLICT UPDATE — plain INSERT OR
                # REPLACE would delete the row first, silently wiping the
                # last_updated timestamp (and any other new column) every time
                # a mod folder's mtime changes.
                db.execute(
                    """INSERT INTO local_mod_metadata
                       (instance_id, folder, name, package_id, workshop_id,
                        supported_versions, dir_mtime, cached_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(instance_id, folder) DO UPDATE SET
                         name = excluded.name,
                         package_id = excluded.package_id,
                         workshop_id = excluded.workshop_id,
                         supported_versions = excluded.supported_versions,
                         dir_mtime = excluded.dir_mtime,
                         cached_at = excluded.cached_at""",
                    (
                        iid,
                        m.folder,
                        m.name,
                        m.package_id,
                        m.workshop_id,
                        json.dumps(m.supported_versions),
                        mtime,
                        now,
                    ),
                )

    return metas


def invalidate_folder(folder: str, instance_id: str | None = None) -> None:
    """Remove a single mod folder from the persistent cache (active instance)."""
    db = get_conn()
    with db:
        db.execute(
            "DELETE FROM local_mod_metadata WHERE instance_id = ? AND folder = ?",
            (instance_id or current_instance_id(), folder),
        )


def invalidate_all(instance_id: str | None = None) -> None:
    """Clear the persistent cache of one instance (the active one by default)."""
    _clear_all(instance_id or current_instance_id())


def _clear_all(instance_id: str) -> None:
    db = get_conn()
    with db:
        db.execute("DELETE FROM local_mod_metadata WHERE instance_id = ?", (instance_id,))
