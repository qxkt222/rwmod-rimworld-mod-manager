"""Mod tags/categories system — user-defined labels for mod organization.

Tags are stored in SQLite, scoped to an instance: the same folder name may
exist under two RimWorld installs with independent tag sets. Each tag is a
simple string label associated with a mod folder name (not workshop ID, since
mods without workshop IDs can also be tagged).

Usage:
    from rwmod.tags import add_tag, remove_tag, get_tags, get_mods_by_tag
"""

from __future__ import annotations

from rwmod.config import current_instance_id
from rwmod.database import get_conn

__all__ = ["add_tag", "remove_tag", "get_tags", "get_mods_by_tag", "list_all_tags"]


def _iid(instance_id: str | None) -> str:
    return instance_id or current_instance_id()


def add_tag(mod_folder: str, tag: str, instance_id: str | None = None) -> bool:
    """Add a tag to a mod. Returns True if added, False if already exists."""
    db = get_conn()
    iid = _iid(instance_id)
    value = tag.strip()
    # Fast path: avoid a write for the common "already tagged" case. The
    # SELECT is only a hint — INSERT OR IGNORE below is what makes a
    # concurrent writer's winning insert harmless (DATA-7).
    cur = db.execute(
        "SELECT 1 FROM mod_tags WHERE instance_id = ? AND folder = ? AND tag = ?",
        (iid, mod_folder, value),
    )
    if cur.fetchone():
        return False
    cur = db.execute(
        "INSERT OR IGNORE INTO mod_tags (instance_id, folder, tag) VALUES (?, ?, ?)",
        (iid, mod_folder, value),
    )
    db.commit()
    return cur.rowcount > 0


def remove_tag(mod_folder: str, tag: str, instance_id: str | None = None) -> bool:
    """Remove a tag from a mod. Returns True if removed."""
    db = get_conn()
    cur = db.execute(
        "DELETE FROM mod_tags WHERE instance_id = ? AND folder = ? AND tag = ?",
        (_iid(instance_id), mod_folder, tag.strip()),
    )
    db.commit()
    return cur.rowcount > 0


def get_tags(mod_folder: str, instance_id: str | None = None) -> list[str]:
    """Get all tags for a mod folder."""
    db = get_conn()
    rows = db.execute(
        "SELECT tag FROM mod_tags WHERE instance_id = ? AND folder = ? ORDER BY tag",
        (_iid(instance_id), mod_folder),
    ).fetchall()
    return [r["tag"] for r in rows]


def get_mods_by_tag(tag: str, instance_id: str | None = None) -> list[str]:
    """Get all mod folder names with a given tag."""
    db = get_conn()
    rows = db.execute(
        "SELECT folder FROM mod_tags WHERE instance_id = ? AND tag = ? ORDER BY folder",
        (_iid(instance_id), tag.strip()),
    ).fetchall()
    return [r["folder"] for r in rows]


def list_all_tags(instance_id: str | None = None) -> list[dict]:
    """List all tags with mod counts."""
    db = get_conn()
    rows = db.execute(
        "SELECT tag, COUNT(*) as count FROM mod_tags WHERE instance_id = ?"
        " GROUP BY tag ORDER BY count DESC",
        (_iid(instance_id),),
    ).fetchall()
    return [{"tag": r["tag"], "count": r["count"]} for r in rows]


def remove_all_tags(mod_folder: str, instance_id: str | None = None) -> int:
    """Remove all tags for a mod folder. Returns count of removed."""
    db = get_conn()
    cur = db.execute(
        "DELETE FROM mod_tags WHERE instance_id = ? AND folder = ?",
        (_iid(instance_id), mod_folder),
    )
    db.commit()
    return cur.rowcount
