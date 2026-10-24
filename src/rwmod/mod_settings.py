"""RimWorld per-mod settings files — ``<Config>/Mod_*.xml``.

RimWorld writes one XML settings file per mod under the game's Config
directory (``%USERPROFILE%/AppData/LocalLow/Ludeon Studios/RimWorld by Ludeon
Studios/Config`` on Windows). This module lists them, reads one raw, writes one
back, and manages backups under ``~/.rwmod/mod_settings_backups/``.

Safety rules:

* only ``Mod_*.xml`` basenames are accepted — every other file in Config
  (``KeyPrefs.xml``, ``Knowledge.xml``, …) is out of scope;
* writes are capped at 2 MB, must be well-formed XML, and require an explicit
  ``ack_game_closed`` acknowledgement (the game overwrites settings on exit);
* the current file is copied to the backup directory *before* every write;
* the BOM and line-ending style of the existing file are preserved;
* every read/write path goes through the Wave-1 sandbox guard
  (``RWMOD_TEST_SANDBOX_HOME``) so a test can never touch the real Config dir;
* process detection only *warns* — it never blocks a write.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess  # nosec B404 — fixed argv, no shell; process probe is best-effort
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from defusedxml import ElementTree as DefusedET

from rwmod.errors import ModNotFoundError, ValidationError
from rwmod.metadata import ModMeta, installed_mod_metas
from rwmod.profile import _assert_within_sandbox

__all__ = [
    "MAX_SETTINGS_BYTES",
    "backup_all",
    "backups_dir",
    "is_game_running",
    "list_backups",
    "list_mod_settings",
    "read_mod_setting",
    "resolve_config_dir",
    "restore_backup",
    "validate_filename",
    "write_mod_setting",
]

_log = logging.getLogger(__name__)

#: Hard cap for a single settings file. RimWorld's own files are kilobytes;
#: the limit stops an accidental 100 MB paste from being written.
MAX_SETTINGS_BYTES = 2 * 1024 * 1024

_UTF8_BOM = b"\xef\xbb\xbf"

# Mod_<anything>.xml, with every Windows-invalid filename character excluded.
_SETTINGS_NAME_RE = re.compile(r'^Mod_[^<>:"/\\|?*\x00-\x1f]{1,200}\.xml$')
# Backups are "<YYYYmmdd>_<HHMMSS>_<ffffff>[_<n>]_<original name>".
_BACKUP_NAME_RE = re.compile(r"^\d{8}_\d{6}_\d{6}(?:_\d+)?_(.+)$")
_NUMERIC_PREFIX_RE = re.compile(r"^([0-9]+)_")


def backups_dir() -> Path:
    """Backup root. Resolved per call so a patched ``Path.home`` is honoured."""
    return Path.home() / ".rwmod" / "mod_settings_backups"


# ── config dir discovery ───────────────────────────────────────────


def resolve_config_dir(config_dir: Path | None) -> Path | None:
    """Return the RimWorld Config directory to use, or None when absent.

    An explicit ``Config.config_dir`` wins. Otherwise the standard Ludeon
    Studios LocalLow location is probed **read-only** — this function never
    creates anything. Both paths pass the sandbox guard, so a test running
    with ``RWMOD_TEST_SANDBOX_HOME`` set fails closed instead of reading the
    developer's real config.
    """
    if config_dir is not None:
        candidate = Path(config_dir)
        _assert_within_sandbox(candidate)
        return candidate if candidate.is_dir() else None

    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "LocalLow" / "Ludeon Studios"
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Ludeon Studios"
    else:
        base = Path.home() / ".config" / "unity3d" / "Ludeon Studios"

    if not base.is_dir():
        return None
    for candidate in sorted(base.glob("RimWorld*")):
        config_path = candidate / "Config"
        if config_path.is_dir():
            _assert_within_sandbox(config_path)
            return config_path
    return None


def validate_filename(filename: str) -> str:
    """Enforce the ``Mod_*.xml`` basename whitelist (no traversal)."""
    if not isinstance(filename, str) or not _SETTINGS_NAME_RE.match(filename):
        raise ValidationError(f"非法的 Mod 设置文件名: {filename!r}")
    if Path(filename).name != filename:
        raise ValidationError(f"非法的 Mod 设置文件名: {filename!r}")
    return filename


# ── listing ────────────────────────────────────────────────────────


def list_mod_settings(config_dir: Path | None, mods_dir: Path | None) -> dict:
    """List ``Mod_*.xml`` files with size, mtime and best-effort mod association.

    Files that cannot be associated with an installed mod are still listed
    (``associated=False``); the UI shows their filename.
    """
    cfg_dir = resolve_config_dir(config_dir)
    if cfg_dir is None:
        return {"config_dir": None, "files": []}

    metas = installed_mod_metas(mods_dir) if mods_dir is not None else []
    files: list[dict] = []
    for path in sorted(cfg_dir.glob("Mod_*.xml")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        association = _associate(path.name, metas)
        files.append(
            {
                "filename": path.name,
                "size": st.st_size,
                "mtime": _iso(st.st_mtime),
                **association,
            }
        )
    return {"config_dir": str(cfg_dir), "files": files}


def _associate(filename: str, metas: Sequence[ModMeta]) -> dict:
    """Map a settings filename to an installed mod.

    RimWorld names the file ``Mod_<workshopId>_<className>.xml`` for Steam mods
    and ``Mod_<packageId>_<className>.xml`` for local mods. packageIds may
    contain underscores, so the packageId branch matches known ids as a prefix
    (case-insensitively, longest match wins) instead of splitting on ``_``.
    """
    rest = filename[len("Mod_") : -len(".xml")] if filename.startswith("Mod_") else filename

    numeric = _NUMERIC_PREFIX_RE.match(rest)
    if numeric:
        workshop_id = numeric.group(1)
        meta = next((m for m in metas if m.workshop_id == workshop_id), None)
        return _association(meta, workshop_id=workshop_id)

    lowered = rest.lower()
    best: ModMeta | None = None
    for meta in metas:
        package_id = meta.package_id.strip()
        if not package_id:
            continue
        needle = package_id.lower()
        if (lowered == needle or lowered.startswith(needle + "_")) and (
            best is None or len(package_id) > len(best.package_id)
        ):
            best = meta
    return _association(best)


def _association(meta: ModMeta | None, workshop_id: str = "") -> dict:
    if meta is None:
        return {
            "associated": False,
            "workshop_id": workshop_id,
            "package_id": "",
            "name": "",
        }
    return {
        "associated": True,
        "workshop_id": meta.workshop_id or workshop_id,
        "package_id": meta.package_id,
        "name": meta.name,
    }


# ── read ───────────────────────────────────────────────────────────


def read_mod_setting(filename: str, config_dir: Path | None) -> dict:
    """Return one settings file's raw XML plus a small structural summary.

    A malformed file is still returned (``well_formed=False``) so the editor
    can show and repair it — only missing/unreadable files are an error.
    """
    target = _existing_target(filename, config_dir)
    raw = target.read_bytes()
    text = _decode(raw, target)

    well_formed = True
    root_tag = ""
    try:
        root_tag = DefusedET.fromstring(text).tag
    except Exception:  # noqa: BLE001 — a corrupt file is data the editor shows
        well_formed = False

    return {
        "filename": target.name,
        "size": len(raw),
        "mtime": _iso(target.stat().st_mtime),
        "xml": text,
        "well_formed": well_formed,
        "root_tag": root_tag,
        "game_running": is_game_running(),
    }


# ── write ──────────────────────────────────────────────────────────


def write_mod_setting(
    filename: str,
    content: str,
    *,
    config_dir: Path | None,
    ack_game_closed: bool,
) -> dict:
    """Validate and atomically replace one settings file (backup first)."""
    if not ack_game_closed:
        raise ValidationError("写入 Mod 设置前必须确认已关闭游戏（ack_game_closed=true）")

    target = _existing_target(filename, config_dir)
    if not isinstance(content, str):
        raise ValidationError("xml 必须是字符串")

    if len(content.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise ValidationError(f"Mod 设置文件过大（上限 {MAX_SETTINGS_BYTES // (1024 * 1024)} MB）")

    body = content.lstrip("﻿")
    _validate_well_formed(body)

    old_raw = target.read_bytes()
    data = _match_style(body, old_raw)

    backup = _backup_file(target)
    _atomic_write(target, data)

    running = is_game_running()
    return {
        "ok": True,
        "filename": target.name,
        "size": len(data),
        "backup": backup.name,
        "game_running": running,
        "warning": _game_warning(running),
    }


def _match_style(text: str, old_raw: bytes) -> bytes:
    """Apply the existing file's BOM and newline style to *text*."""
    has_bom = old_raw.startswith(_UTF8_BOM)
    use_crlf = b"\r\n" in old_raw

    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if use_crlf:
        normalised = normalised.replace("\n", "\r\n")

    data = normalised.encode("utf-8")
    return (_UTF8_BOM + data) if has_bom else data


def _validate_well_formed(text: str) -> None:
    try:
        DefusedET.fromstring(text)
    except Exception as e:  # noqa: BLE001 — every parse failure is a 400
        raise ValidationError(f"XML 格式无效: {e}") from e


def _atomic_write(target: Path, data: bytes) -> None:
    _assert_within_sandbox(target)
    tmp = target.with_name(target.name + ".rwmod.tmp")
    try:
        tmp.write_bytes(data)
        import os

        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# ── backups ────────────────────────────────────────────────────────


def backup_all(config_dir: Path | None) -> dict:
    """Copy every ``Mod_*.xml`` into the backup directory."""
    cfg_dir = resolve_config_dir(config_dir)
    root = backups_dir()
    if cfg_dir is None:
        return {"ok": True, "count": 0, "backup_dir": str(root), "backups": []}

    rows: list[dict] = []
    for path in sorted(cfg_dir.glob("Mod_*.xml")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        backup = _backup_file(path)
        rows.append({"filename": path.name, "backup": backup.name, "size": size})

    _log.info("已备份 %d 个 Mod 设置文件 → %s", len(rows), root)
    return {"ok": True, "count": len(rows), "backup_dir": str(root), "backups": rows}


def list_backups() -> dict:
    """List settings backups, newest first."""
    root = backups_dir()
    if not root.is_dir():
        return {"backup_dir": str(root), "backups": []}

    rows: list[dict] = []
    for path in root.glob("*.xml"):
        source = _backup_source(path.name)
        if not source or not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        rows.append(
            {
                "filename": path.name,
                "source": source,
                "size": st.st_size,
                "mtime": _iso(st.st_mtime),
            }
        )
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return {"backup_dir": str(root), "backups": rows}


def restore_backup(
    filename: str,
    backup: str = "",
    *,
    config_dir: Path | None,
    ack_game_closed: bool,
) -> dict:
    """Restore one settings file from a backup (newest one when *backup* empty).

    The current file is itself backed up first, so a restore is undoable.
    """
    if not ack_game_closed:
        raise ValidationError("恢复 Mod 设置前必须确认已关闭游戏（ack_game_closed=true）")

    target = _existing_target(filename, config_dir)
    source = _pick_backup(filename, backup)

    data = source.read_bytes()
    _validate_well_formed(_decode(data, source))

    backup_of_current = _backup_file(target)
    _atomic_write(target, data)

    running = is_game_running()
    return {
        "ok": True,
        "filename": target.name,
        "restored_from": source.name,
        "backup": backup_of_current.name,
        "game_running": running,
        "warning": _game_warning(running),
    }


def _pick_backup(filename: str, backup: str) -> Path:
    root = backups_dir()
    if backup:
        if Path(backup).name != backup or not _BACKUP_NAME_RE.match(backup):
            raise ValidationError(f"非法的备份文件名: {backup!r}")
        if _backup_source(backup) != filename:
            raise ValidationError(f"备份 {backup!r} 不属于 {filename!r}")
        source = root / backup
        _assert_within_sandbox(source)
        if not source.is_file():
            raise ModNotFoundError(f"备份不存在: {backup}")
        return source

    if not root.is_dir():
        raise ModNotFoundError(f"没有找到 {filename} 的备份")
    # Iterate instead of globbing: a legitimate filename may contain glob
    # metacharacters (e.g. "Mod_[Foo]_Bar.xml"), which would never match itself
    # as a pattern.
    candidates = [
        p
        for p in root.iterdir()
        if p.is_file() and not p.is_symlink() and _backup_source(p.name) == filename
    ]
    if not candidates:
        raise ModNotFoundError(f"没有找到 {filename} 的备份")
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)


def _backup_file(source: Path) -> Path:
    """Copy *source* into the backup directory and return the new path."""
    _assert_within_sandbox(source)
    root = backups_dir()
    _assert_within_sandbox(root)
    root.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    dest = root / f"{ts}_{source.name}"
    counter = 1
    while dest.exists():
        dest = root / f"{ts}_{counter}_{source.name}"
        counter += 1
    shutil.copy2(source, dest)
    return dest


def _backup_source(name: str) -> str:
    match = _BACKUP_NAME_RE.match(name)
    return match.group(1) if match else ""


# ── helpers ────────────────────────────────────────────────────────


def _existing_target(filename: str, config_dir: Path | None) -> Path:
    """Validate the name and return the existing file (404 when missing)."""
    filename = validate_filename(filename)
    cfg_dir = resolve_config_dir(config_dir)
    if cfg_dir is None:
        raise ModNotFoundError("未找到 RimWorld Config 目录")
    target = cfg_dir / filename
    if target.is_symlink() or not target.is_file():
        raise ModNotFoundError(f"Mod 设置文件不存在: {filename}")
    _assert_within_sandbox(target)
    return target


def _decode(raw: bytes, path: Path) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValidationError(f"无法以 UTF-8 读取 {path.name}: {e}") from e


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def is_game_running() -> bool:
    """Best-effort: is a RimWorld process running? Never raises.

    The result is advisory only — endpoints warn but never refuse a write.
    """
    try:
        if platform.system() == "Windows":
            for exe in ("RimWorldWin64.exe", "RimWorldWin.exe"):
                completed = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
                    ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if exe.lower() in (completed.stdout or "").lower():
                    return True
            return False

        completed = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
            ["pgrep", "-f", "RimWorld"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return completed.returncode == 0
    except Exception:  # noqa: BLE001 — a probe failure means "unknown", not "crash"
        return False


def _game_warning(running: bool) -> str:
    if not running:
        return ""
    return "检测到 RimWorld 正在运行：请先退出游戏，否则游戏退出时会覆盖本次修改"
