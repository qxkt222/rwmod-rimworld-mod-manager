"""One-click export/import — package profiles, backups, tags & config.

Produces a single ``.rwmod`` zip that can be moved to another machine and
imported to restore the full rwmod setup (minus the actual mod files, which
are re-downloaded from Steam).
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from rwmod.config import Config
from rwmod.profile import PROFILES_DIR
from rwmod.utils import safe_extract_zip

_log = logging.getLogger(__name__)

__all__ = ["export_bundle", "import_bundle"]

# Internal layout of the .rwmod bundle
_MANIFEST = "manifest.json"
_PROFILES = "profiles/"
_BACKUPS = "backups/"
_TAGS = "tags.json"
_CONFIG = "config.toml"


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
    manifest: dict[str, object] = {
        "app": "rwmod",
        "version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "profiles": 0,
        "backups": 0,
        "tags": False,
        "config": True,
        "has_steam_api_key": bool(cfg.steam_api_key and not include_secrets),
    }

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Profiles
        if PROFILES_DIR.exists():
            for f in sorted(PROFILES_DIR.glob("*.xml")):
                zf.write(f, f"{_PROFILES}{f.name}")
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
        # 1. Profiles
        profiles_imported = 0
        src_profiles = tmp_dir / _PROFILES
        if src_profiles.exists():
            PROFILES_DIR.mkdir(parents=True, exist_ok=True)
            for f in src_profiles.glob("*.xml"):
                shutil.copy2(f, PROFILES_DIR / f.name)
                profiles_imported += 1

        # 2. Backups
        backups_imported = 0
        src_backups = tmp_dir / _BACKUPS
        if src_backups.exists():
            cfg.backup_dir.mkdir(parents=True, exist_ok=True)
            for f in src_backups.glob("*.zip"):
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


def _export_tags() -> dict | None:
    """Read all folder→tags mappings from the tags DB. Returns None if unavailable."""
    try:
        from rwmod.database import get_conn

        db = get_conn()
        rows = db.execute("SELECT folder, tag FROM mod_tags ORDER BY folder, tag").fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r["folder"], []).append(r["tag"])
        return result
    except Exception:
        return None


def _import_tags(tags: dict) -> None:
    """Write folder→tags mappings into the tags DB."""
    try:
        from rwmod.database import get_conn

        db = get_conn()
        for folder, tag_list in tags.items():
            for tag in tag_list:
                db.execute(
                    "INSERT OR IGNORE INTO mod_tags (folder, tag) VALUES (?, ?)",
                    (folder, tag),
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
