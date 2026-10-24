"""One-click export/import — package profiles, backups, tags & config.

Produces a single ``.rwmod`` zip that can be moved to another machine and
imported to restore the full rwmod setup (minus the actual mod files, which
are re-downloaded from Steam).

Bundle format v2 scopes profiles and tags to instances::

    manifest.json   {"version": 2, "instances": [{"id": ..., "name": ...}]}
    profiles/<instance_id>/<name>.xml
    tags.json       {"version": 2, "instances": {<id>: {folder: [tags]}}}

Version-1 bundles (profiles at the zip root, a flat ``folder -> tags`` map)
are still importable; everything in them belongs to the ``default`` instance.
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from rwmod.config import Config, is_valid_instance_id
from rwmod.profile import PROFILES_DIR, profiles_dir_for
from rwmod.utils import safe_extract_zip

_log = logging.getLogger(__name__)

__all__ = ["export_bundle", "import_bundle"]

# Internal layout of the .rwmod bundle
_MANIFEST = "manifest.json"
_PROFILES = "profiles/"
_BACKUPS = "backups/"
_TAGS = "tags.json"
_CONFIG = "config.toml"
_BUNDLE_VERSION = 2


def export_bundle(
    cfg: Config, output_path: Path, include_backups: bool = True, include_secrets: bool = False
) -> dict:
    """Package profiles, tags, config (and optionally backups) into a .rwmod zip.

    ``include_secrets`` controls whether the Steam API key is bundled. It is
    OFF by default: the .rwmod file is meant to be shared between machines,
    and the API key is a credential. When omitted the manifest records
    ``"has_steam_api_key"`` so the importing side can tell the user a key was
    deliberately left behind.

    Returns {"ok": bool, "path": str, "profiles": int, "backups": int, "size_mb": float}.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    profiles_count = 0
    backups_count = 0
    instance_ids = _profile_instance_ids(cfg)
    manifest: dict[str, object] = {
        "app": "rwmod",
        "version": _BUNDLE_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "profiles": 0,
        "backups": 0,
        "tags": False,
        "config": True,
        "has_steam_api_key": bool(cfg.steam_api_key and not include_secrets),
        "instances": [{"id": iid, "name": _instance_name(cfg, iid)} for iid in instance_ids],
    }

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Profiles, one directory per instance.
        for iid in instance_ids:
            directory = profiles_dir_for(iid, create=False)
            if not directory.exists():
                continue
            for f in sorted(directory.glob("*.xml")):
                if not f.is_file():
                    continue
                zf.write(f, f"{_PROFILES}{iid}/{f.name}")
                profiles_count += 1

        # 2. Backups (optional — can be large)
        if include_backups and cfg.backup_dir.exists():
            for f in sorted(cfg.backup_dir.glob("*.zip")):
                zf.write(f, f"{_BACKUPS}{f.name}")
                backups_count += 1

        # 3. Tags (from the tags DB, if present)
        tags = _export_tags()
        if tags is not None:
            zf.writestr(_TAGS, json.dumps(tags, ensure_ascii=False, indent=2))
            manifest["tags"] = True

        # 4. Config (paths are stripped of machine-specific values)
        zf.writestr(_CONFIG, _export_config(cfg, include_secrets))

        manifest["profiles"] = profiles_count
        manifest["backups"] = backups_count
        zf.writestr(_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))

    size_mb = round(output_path.stat().st_size / 1024 / 1024, 2)
    _log.info(
        "导出完成: %s (%d profiles, %d backups, %.2f MB)",
        output_path,
        profiles_count,
        backups_count,
        size_mb,
    )
    return {
        "ok": True,
        "path": str(output_path),
        "profiles": profiles_count,
        "backups": backups_count,
        "tags": manifest["tags"],
        "size_mb": size_mb,
    }


def import_bundle(cfg: Config, bundle_path: Path) -> dict:
    """Import a .rwmod bundle, restoring profiles, tags, config & backups.

    Returns {"ok": bool, "msg": str, "profiles": int, "backups": int}.
    """
    if not bundle_path.exists():
        return {"ok": False, "msg": f"文件不存在: {bundle_path}"}

    tmp_dir = bundle_path.parent / f".rwmod_import_{datetime.now(UTC).strftime('%H%M%S')}"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bundle_path, "r") as zf:
            safe_extract_zip(zf, tmp_dir)
    except (zipfile.BadZipFile, OSError) as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return {"ok": False, "msg": f"解压失败: {e}"}

    try:
        # 1. Profiles — v2 bundles keep them under profiles/<instance>/,
        # v1 bundles at the root (→ default instance).
        profiles_imported = 0
        src_profiles = tmp_dir / _PROFILES
        if src_profiles.exists():
            for entry in sorted(src_profiles.rglob("*")):
                rel = entry.relative_to(src_profiles)
                parts = rel.parts
                if entry.is_dir():
                    # A zip may carry a directory entry named "dir.xml/":
                    # copying it would raise IsADirectoryError and used to
                    # crash the whole import (DATA-3).
                    if entry.name.endswith(".xml"):
                        return {
                            "ok": False,
                            "msg": f"bundle 包含非文件条目: profiles/{rel.as_posix()}",
                        }
                    continue
                if not entry.name.endswith(".xml"):
                    continue
                if len(parts) == 1:
                    iid, filename = "default", parts[0]
                elif len(parts) == 2:
                    iid, filename = parts
                else:
                    # Older importers ignored nested directories; keep doing so.
                    continue
                if not is_valid_instance_id(iid):
                    return {"ok": False, "msg": f"bundle 包含非法实例 id: {iid!r}"}
                dest_dir = profiles_dir_for(iid)
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, dest_dir / filename)
                profiles_imported += 1

        # 2. Backups
        backups_imported = 0
        src_backups = tmp_dir / _BACKUPS
        if src_backups.exists():
            cfg.backup_dir.mkdir(parents=True, exist_ok=True)
            for f in src_backups.glob("*.zip"):
                if not f.is_file():
                    return {"ok": False, "msg": f"bundle 包含非文件条目: backups/{f.name}"}
                shutil.copy2(f, cfg.backup_dir / f.name)
                backups_imported += 1

        # 3. Tags
        tags_file = tmp_dir / _TAGS
        if tags_file.exists():
            try:
                tags = json.loads(tags_file.read_text(encoding="utf-8"))
                _import_tags(tags)
            except Exception as e:
                _log.warning("导入标签失败: %s", e)

        # 4. Config — only restore non-path settings (steam_api_key)
        config_file = tmp_dir / _CONFIG
        if config_file.exists():
            _import_config(cfg, config_file)

        return {
            "ok": True,
            "msg": f"导入完成: {profiles_imported} 个 Profile, {backups_imported} 个备份",
            "profiles": profiles_imported,
            "backups": backups_imported,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── internal helpers ───────────────────────────────────────────────


def _profile_instance_ids(cfg: Config) -> list[str]:
    """Configured instance ids plus any instance dir found on disk."""
    ids = [inst.id for inst in cfg.instances]
    try:
        base = Path(PROFILES_DIR)
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir() and is_valid_instance_id(child.name) and child.name not in ids:
                    ids.append(child.name)
    except OSError:  # pragma: no cover — unreadable profiles dir
        pass
    return ids


def _instance_name(cfg: Config, instance_id: str) -> str:
    inst = cfg.get_instance(instance_id)
    return inst.name if inst is not None else instance_id


def _export_tags() -> dict | None:
    """Read instance→folder→tags from the DB. Returns None if unavailable."""
    try:
        from rwmod.database import get_conn

        db = get_conn()
        rows = db.execute(
            "SELECT instance_id, folder, tag FROM mod_tags ORDER BY instance_id, folder, tag"
        ).fetchall()
        result: dict[str, dict[str, list[str]]] = {}
        for r in rows:
            result.setdefault(r["instance_id"], {}).setdefault(r["folder"], []).append(r["tag"])
        return {"version": _BUNDLE_VERSION, "instances": result}
    except Exception:
        return None


def _import_tags(tags: object) -> None:
    """Write folder→tags mappings into the tags DB (v1 flat or v2 scoped)."""
    try:
        from rwmod.database import get_conn

        if not isinstance(tags, dict):
            return
        pairs: list[tuple[str, str, str]] = []
        scoped = tags.get("instances")
        if isinstance(scoped, dict):
            for iid, mapping in scoped.items():
                iid_s = str(iid)
                if not is_valid_instance_id(iid_s) or not isinstance(mapping, dict):
                    continue
                for folder, tag_list in mapping.items():
                    if not isinstance(tag_list, list):
                        continue
                    for tag in tag_list:
                        if isinstance(tag, str):
                            pairs.append((iid_s, str(folder), tag))
        else:
            for folder, tag_list in tags.items():
                if not isinstance(tag_list, list):
                    continue
                for tag in tag_list:
                    if isinstance(tag, str):
                        pairs.append(("default", str(folder), tag))

        db = get_conn()
        for iid, folder, tag in pairs:
            db.execute(
                "INSERT OR IGNORE INTO mod_tags (instance_id, folder, tag) VALUES (?, ?, ?)",
                (iid, folder, tag),
            )
        db.commit()
    except Exception as e:
        _log.warning("写入标签失败: %s", e)


def _export_config(cfg: Config, include_secrets: bool = False) -> str:
    """Serialize config, omitting machine-specific paths and (by default) secrets.

    The Steam API key is a credential — it is only written when the caller
    explicitly opts in (include_secrets=True), e.g. migrating between machines
    the operator owns. For the shared-bundle default it is omitted entirely.
    """
    lines = []
    if cfg.steam_api_key and include_secrets:
        lines.append(f'steam_api_key = "{cfg.steam_api_key}"')
    return "\n".join(lines) + "\n"


def _import_config(cfg: Config, config_file: Path) -> None:
    """Restore non-path config values (steam_api_key) into the live config."""
    try:
        import tomllib

        data = tomllib.loads(config_file.read_text(encoding="utf-8"))
        key = data.get("steam_api_key", "")
        if key and key != cfg.steam_api_key:
            cfg.steam_api_key = key
            cfg.save()
            _log.info("已恢复 steam_api_key")
    except Exception as e:
        _log.warning("恢复配置失败: %s", e)
