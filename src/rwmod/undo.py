"""Operation undo — snapshot ModsConfig.xml before destructive actions.

Destructive operations (auto-sort, profile restore, backup restore) modify
the active ModsConfig.xml. Before each one we snapshot the current file into
~/.rwmod/undo/, so the user can roll back to the pre-operation state with a
single click.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

__all__ = ["snapshot_modsconfig", "undo_last", "list_undo_snapshots"]

UNDO_DIR = Path.home() / ".rwmod" / "undo"
_MAX_SNAPSHOTS = 20


def snapshot_modsconfig(modsconfig_path: Path, label: str = "") -> Path | None:
    """Snapshot the current ModsConfig.xml before a destructive operation.

    Returns the snapshot path, or None if the source doesn't exist.
    """
    if not modsconfig_path.exists():
        return None
    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c for c in label if c.isalnum() or c in "-_")[:40]
    name = f"{ts}_{safe_label}.xml" if safe_label else f"{ts}.xml"
    dest = UNDO_DIR / name
    shutil.copy2(modsconfig_path, dest)
    _prune()
    _log.info("撤销快照: %s → %s", modsconfig_path.name, dest.name)
    return dest


def undo_last(modsconfig_path: Path) -> dict:
    """Restore the most recent snapshot to modsconfig_path.

    Returns {"ok": bool, "msg": str, "restored": str|None}.
    """
    snapshots = list_undo_snapshots()
    if not snapshots:
        return {"ok": False, "msg": "没有可撤销的操作", "restored": None}

    latest = snapshots[0]
    try:
        modsconfig_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(latest, modsconfig_path)
        latest.unlink(missing_ok=True)
        _log.info("已撤销: %s → %s", latest.name, modsconfig_path)
        return {"ok": True, "msg": f"已撤销到 {latest.name}", "restored": latest.name}
    except OSError as e:
        return {"ok": False, "msg": f"撤销失败: {e}", "restored": None}


def list_undo_snapshots() -> list[Path]:
    """List undo snapshots, newest first."""
    if not UNDO_DIR.exists():
        return []
    return sorted(UNDO_DIR.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)


def _prune() -> None:
    """Keep only the most recent _MAX_SNAPSHOTS."""
    snapshots = list_undo_snapshots()
    for old in snapshots[_MAX_SNAPSHOTS:]:
        old.unlink(missing_ok=True)
