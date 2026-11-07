"""Operation undo — snapshot ModsConfig.xml before destructive actions.

Destructive operations (auto-sort, profile restore, backup restore) modify
the active ModsConfig.xml. Before each one we snapshot the current file into
``~/.rwmod/undo/<instance_id>/``, so the user can roll back to the
pre-operation state with a single click. Snapshots are per instance: undoing
after switching installs must not restore another install's mod list.

Legacy snapshots written directly into ``~/.rwmod/undo/`` are migrated into
the ``default/`` instance directory on first use.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

__all__ = ["undo_dir_for", "snapshot_modsconfig", "undo_last", "list_undo_snapshots"]

UNDO_DIR = Path.home() / ".rwmod" / "undo"
_MAX_SNAPSHOTS = 20


def undo_dir_for(instance_id: str | None = None, *, create: bool = True) -> Path:
    """Return ``<UNDO_DIR>/<instance_id>``, migrating legacy root snapshots."""
    from rwmod.config import current_instance_id, is_valid_instance_id

    iid = instance_id or current_instance_id()
    if not is_valid_instance_id(iid):
        raise ValueError(f"非法实例 id: {iid!r}")
    base = Path(UNDO_DIR)
    dest = base / iid
    _migrate_legacy_snapshots(base)
    if create:
        dest.mkdir(parents=True, exist_ok=True)
    return dest


def _migrate_legacy_snapshots(base: Path) -> None:
    """Move pre-multi-instance ``base/*.xml`` into ``base/default/`` exactly once."""
    if not base.is_dir():
        return
    legacy = [p for p in base.glob("*.xml") if p.is_file()]
    if not legacy:
        return
    dest_dir = base / "default"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in legacy:
        dest = dest_dir / src.name
        suffix = 2
        while dest.exists():
            dest = dest_dir / f"{src.stem}-legacy{suffix}{src.suffix}"
            suffix += 1
        try:
            os.replace(src, dest)
        except OSError:  # pragma: no cover — a racing third party moved it first
            continue


def snapshot_modsconfig(
    modsconfig_path: Path, label: str = "", instance_id: str | None = None
) -> Path | None:
    """Snapshot the current ModsConfig.xml before a destructive operation.

    Returns the snapshot path, or None if the source doesn't exist.
    """
    if not modsconfig_path.exists():
        return None
    directory = undo_dir_for(instance_id)
    # Microsecond resolution + a collision suffix: two operations inside the
    # same wall-clock second must be two distinct undo steps (DATA-5b).
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    safe_label = "".join(c for c in label if c.isalnum() or c in "-_")[:40]
    stem = f"{ts}_{safe_label}" if safe_label else ts
    dest = directory / f"{stem}.xml"
    suffix = 1
    while dest.exists():
        dest = directory / f"{stem}_{suffix}.xml"
        suffix += 1
    # copyfile, not copy2: the snapshot's mtime must record when the snapshot
    # was taken. copy2 would inherit the source's mtime, making "newest first"
    # (and _prune) depend on how old ModsConfig.xml was (DATA-5a).
    shutil.copyfile(modsconfig_path, dest)
    _prune(instance_id)
    _log.info("撤销快照: %s → %s", modsconfig_path.name, dest.name)
    return dest


def _atomic_copy(source: Path, target: Path) -> None:
    """Replace *target* with a copy of *source* via a same-directory temp file.

    ``shutil.copy2(source, target)`` truncates the live ModsConfig.xml before
    writing it, so a crash / full disk / killed process mid-copy leaves RimWorld
    with a truncated (unreadable) mod list and no way back. Writing a sibling
    temp file and renaming it with ``os.replace`` means the live file is either
    the old one or the complete new one — never a half-written amalgam.

    The temp name comes from :func:`tempfile.mkstemp`, so two concurrent
    restores cannot share (and corrupt) one scratch file (REV-010).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{target.name}.", suffix=".rwmod.tmp", dir=target.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def undo_last(modsconfig_path: Path, instance_id: str | None = None) -> dict:
    """Restore the most recent snapshot of one instance to modsconfig_path.

    Returns ``{"ok": bool, "msg": str, "restored": str|None}``. The router
    turns ``ok: False`` into the matching ``errors.py`` exception, so a 200
    body never has to carry a failure flag.
    """
    snapshots = list_undo_snapshots(instance_id)
    if not snapshots:
        return {"ok": False, "msg": "没有可撤销的操作", "restored": None}

    latest = snapshots[0]
    try:
        _atomic_copy(latest, modsconfig_path)
        latest.unlink(missing_ok=True)
        _log.info("已撤销: %s → %s", latest.name, modsconfig_path)
        return {"ok": True, "msg": f"已撤销到 {latest.name}", "restored": latest.name}
    except OSError as e:
        return {"ok": False, "msg": f"撤销失败: {e}", "restored": None}


def list_undo_snapshots(instance_id: str | None = None) -> list[Path]:
    """List undo snapshots of one instance, newest first."""
    directory = undo_dir_for(instance_id, create=False)
    if not directory.exists():
        return []
    # Tie-break on the microsecond-stamped filename: filesystem mtime
    # granularity can collapse snapshots taken in quick succession.
    return sorted(
        directory.glob("*.xml"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )


def _prune(instance_id: str | None = None) -> None:
    """Keep only the most recent _MAX_SNAPSHOTS of the instance."""
    snapshots = list_undo_snapshots(instance_id)
    for old in snapshots[_MAX_SNAPSHOTS:]:
        old.unlink(missing_ok=True)
