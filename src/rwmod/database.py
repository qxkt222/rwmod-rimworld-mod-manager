"""SQLite persistence layer — download history, mod metadata cache.

Uses a per-thread connection pool to avoid per-query open/close overhead
while still being safe across FastAPI's threadpool workers. Reads and writes
go through WAL mode so concurrent access is efficient without a global lock.

Schema versioning: ``PRAGMA user_version`` tracks the schema level. ``init_db``
runs the base DDL then applies any pending migrations in order, so existing
databases upgrade in place (no data loss) and fresh databases build the full
schema in one pass.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

__all__ = [
    "DB_PATH",
    "SCHEMA_VERSION",
    "init_db",
    "get_conn",
    "record_download",
    "get_download_history",
    "get_download_stats",
    "clear_history",
    "cache_mod",
    "get_cached_mod",
    "find_local_mods_by_workshop_id",
    "upsert_local_mod_workshop_id",
    "remove_local_mod_workshop_id",
    "get_last_updated",
    "set_last_updated",
]

DB_PATH = Path.home() / ".rwmod.db"

# Current schema version. Bump when adding a new migration to _MIGRATIONS.
SCHEMA_VERSION = 1

# Ordered schema migrations. Each entry is (target_version, SQL script).
# init_db() applies every migration whose version is > the database's current
# PRAGMA user_version, inside a transaction — an interrupted migration rolls
# back cleanly and re-runs on the next start.
_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- v1: mod "last checked/updated" timestamp moves from a marker file
        -- inside each mod folder (.rwmod_last_updated) into the metadata DB.
        ALTER TABLE local_mod_metadata ADD COLUMN last_updated INTEGER DEFAULT 0;
        """,
    ),
]

# Per-thread connections: each thread gets its own sqlite3.Connection so
# concurrent FastAPI workers never contend on a single connection.
_local: threading.local = threading.local()
# Guard for the lazily-created initial connection during init_db / close.
_init_lock = threading.Lock()
_initialized = False
# Registry of every connection ever created, so close_db() can close the
# connections owned by *other* threads (e.g. FastAPI's threadpool workers or
# TestClient's portal threads). Without this, those threads' connections leak
# until process exit (observed as "ResourceWarning: unclosed database").
_all_conns: set[sqlite3.Connection] = set()
# Connections closed by close_db() — sqlite3.Connection has no __dict__, so
# we can't tag the object; track by id() so threads that still hold a
# reference rebuild on their next get_conn() instead of hitting
# "Cannot operate on a closed database".
_closed_conn_ids: set[int] = set()


def get_conn() -> sqlite3.Connection:
    """Return a per-thread persistent connection, creating it lazily."""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    # close_db() closes every registered connection from the shutting-down
    # thread; other threads still hold a reference to the now-closed object.
    # Track closed conns by id() so those threads rebuild on next use instead
    # of hitting "Cannot operate on a closed database".
    if conn is not None and id(conn) in _closed_conn_ids:
        _local.conn = None
        conn = None
    if conn is not None:
        return conn
    # NOTE: do NOT call get_conn() while holding _init_lock — the PRAGMA
    # below can block on other connections and _init_lock is non-reentrant,
    # which deadlocks init_db() (it calls get_conn inside the same lock).
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-8000")  # 8MB page cache
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
    # Writes can still contend on the write lock; wait up to 5s instead of failing.
    conn.execute("PRAGMA busy_timeout=5000")
    with _init_lock:
        _all_conns.add(conn)
    _local.conn = conn
    return conn


def close_db() -> None:
    """Close every per-thread connection. Called on shutdown."""
    global _initialized
    with _init_lock:
        _initialized = False
        conns = list(_all_conns)
        _all_conns.clear()
    if getattr(_local, "conn", None) is not None:
        _local.conn = None
    for conn in conns:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        finally:
            _closed_conn_ids.add(id(conn))


def init_db() -> None:
    global _initialized
    # Get the connection OUTSIDE the lock: get_conn() itself takes _init_lock
    # (to register in _all_conns) — calling it here inside the lock would
    # deadlock (non-reentrant). PRAGMAs in get_conn can also block on other
    # threads' connections.
    db = get_conn()
    with _init_lock:
        if _initialized:
            return
        _apply_migrations(db)
        _initialized = True


def _apply_migrations(db: sqlite3.Connection) -> None:
    """Create the base schema, then apply any pending migrations.

    ``PRAGMA user_version`` records the applied schema level; every migration
    in _MIGRATIONS with a higher version is run inside its own transaction.
    A fresh database gets the base DDL then all migrations in order.
    """
    db.executescript(
        """
            CREATE TABLE IF NOT EXISTS download_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                workshop_id TEXT    NOT NULL,
                mod_name    TEXT    DEFAULT '',
                package_id  TEXT    DEFAULT '',
                status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|success|failed|skipped
                msg         TEXT    DEFAULT '',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS mod_cache (
                workshop_id  TEXT PRIMARY KEY,
                title        TEXT    DEFAULT '',
                author       TEXT    DEFAULT '',
                description  TEXT    DEFAULT '',
                time_updated INTEGER DEFAULT 0,
                cached_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS local_mod_metadata (
                folder       TEXT PRIMARY KEY,
                name         TEXT    NOT NULL,
                package_id   TEXT    DEFAULT '',
                workshop_id  TEXT    DEFAULT '',
                dir_mtime    REAL    DEFAULT 0,
                cached_at    REAL    DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_dl_workshop ON download_history(workshop_id);
            CREATE INDEX IF NOT EXISTS idx_dl_created   ON download_history(created_at);
            CREATE INDEX IF NOT EXISTS idx_lmm_workshop ON local_mod_metadata(workshop_id);

            CREATE TABLE IF NOT EXISTS mod_tags (
                folder TEXT NOT NULL,
                tag    TEXT NOT NULL,
                PRIMARY KEY (folder, tag)
            );

            CREATE TABLE IF NOT EXISTS download_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                workshop_id TEXT    NOT NULL UNIQUE,
                name        TEXT    DEFAULT '',
                status      TEXT    NOT NULL DEFAULT 'pending',
                progress    REAL    DEFAULT 0.0,
                msg         TEXT    DEFAULT '',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_dq_status ON download_queue(status);
        """
    )
    db.commit()

    current = db.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in _MIGRATIONS:
        if version <= current:
            continue
        db.execute("BEGIN")
        try:
            db.executescript(sql)
            db.execute(f"PRAGMA user_version = {version}")
            db.commit()
        except Exception:
            db.rollback()
            raise


def record_download(
    workshop_id: str,
    status: str,
    mod_name: str = "",
    package_id: str = "",
    msg: str = "",
) -> int:
    db = get_conn()
    sql = (
        "INSERT INTO download_history"
        " (workshop_id, mod_name, package_id, status, msg)"
        " VALUES (?,?,?,?,?)"
    )
    cur = db.execute(sql, (workshop_id, mod_name, package_id, status, msg))
    db.commit()
    return cur.lastrowid or -1


def get_download_history(limit: int = 50, status: str = "") -> list[dict]:
    db = get_conn()
    query = "SELECT * FROM download_history"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def cache_mod(
    workshop_id: str,
    title: str = "",
    author: str = "",
    description: str = "",
    time_updated: int = 0,
) -> None:
    db = get_conn()
    db.execute(
        """INSERT OR REPLACE INTO mod_cache (workshop_id, title, author, description, time_updated, cached_at)
           VALUES (?,?,?,?,?,datetime('now'))""",
        (workshop_id, title, author, description, time_updated),
    )
    db.commit()


def get_cached_mod(workshop_id: str) -> dict | None:
    db = get_conn()
    row = db.execute("SELECT * FROM mod_cache WHERE workshop_id = ?", (workshop_id,)).fetchone()
    return dict(row) if row else None


def get_download_stats() -> dict:
    db = get_conn()
    total = db.execute("SELECT COUNT(*) as n FROM download_history").fetchone()["n"]
    success = db.execute(
        "SELECT COUNT(*) as n FROM download_history WHERE status='success'"
    ).fetchone()["n"]
    failed = db.execute(
        "SELECT COUNT(*) as n FROM download_history WHERE status='failed'"
    ).fetchone()["n"]
    return {"total": total, "success": success, "failed": failed}


def clear_history() -> None:
    db = get_conn()
    db.execute("DELETE FROM download_history")
    db.commit()


# ── local mod workshop_id index (folder → workshop_id) ──────────────
# Used by downloader._find_existing to avoid a full directory scan on every
# download. The index lives in local_mod_metadata.workshop_id; folder names
# are the primary key. After a download we upsert the workshop_id so subsequent
# lookups are O(1) SQLite queries instead of O(n) directory scans.


def find_local_mods_by_workshop_id(workshop_id: str) -> list[str]:
    """Return folder names whose PublishedFileId matches workshop_id."""
    db = get_conn()
    rows = db.execute(
        "SELECT folder FROM local_mod_metadata WHERE workshop_id = ?",
        (workshop_id,),
    ).fetchall()
    return [r["folder"] for r in rows]


def upsert_local_mod_workshop_id(folder: str, workshop_id: str) -> None:
    """Record (folder → workshop_id) mapping in the metadata cache index."""
    db = get_conn()
    db.execute(
        """INSERT INTO local_mod_metadata (folder, name, workshop_id, dir_mtime, cached_at)
           VALUES (?, '', ?, 0, ?)
           ON CONFLICT(folder) DO UPDATE SET workshop_id = excluded.workshop_id""",
        (folder, workshop_id, time.time()),
    )
    db.commit()


def remove_local_mod_workshop_id(folder: str) -> None:
    """Remove a folder mapping (e.g. after force-reinstall deletes a folder)."""
    db = get_conn()
    db.execute("DELETE FROM local_mod_metadata WHERE folder = ?", (folder,))
    db.commit()


# ── last-updated timestamp (migrated from per-mod .rwmod_last_updated) ──


def get_last_updated(folder: str) -> int:
    """Return the stored update-check timestamp for a mod folder (0 if unset)."""
    db = get_conn()
    row = db.execute(
        "SELECT last_updated FROM local_mod_metadata WHERE folder = ?", (folder,)
    ).fetchone()
    if row is None or row["last_updated"] is None:
        return 0
    return int(row["last_updated"])


def set_last_updated(folder: str, timestamp: int) -> None:
    """Record when a mod folder was last compared against Steam Workshop.

    Upserts so a freshly downloaded mod (no metadata row yet) gets its
    timestamp recorded too.
    """
    db = get_conn()
    db.execute(
        """INSERT INTO local_mod_metadata (folder, name, last_updated)
           VALUES (?, '', ?)
           ON CONFLICT(folder) DO UPDATE SET last_updated = excluded.last_updated""",
        (folder, timestamp),
    )
    db.commit()


# ── queue persistence ──────────────────────────────────────────────


# Whitelist of columns queue_upsert may update — drops unknown keys so caller
# input can never reach the SQL as identifiers (B608).
_QUEUE_UPDATE_COLUMNS = frozenset({"name", "status", "progress", "msg"})


def queue_upsert(workshop_id: str, **kwargs: object) -> None:
    """Insert or update a queue item. kwargs: name, status, progress, msg."""
    db = get_conn()
    safe = {k: v for k, v in kwargs.items() if k in _QUEUE_UPDATE_COLUMNS}
    name = safe.get("name", "")
    status = safe.get("status", "pending")
    progress = safe.get("progress", 0.0)
    msg = safe.get("msg", "")

    sets = [f"{k} = ?" for k in safe]
    params: list = [workshop_id, name, status, progress, msg]
    params.extend(safe[k] for k in safe)

    sets.append("updated_at = datetime('now')")
    db.execute(
        f"INSERT INTO download_queue (workshop_id, name, status, progress, msg)"  # nosec B608 — keys whitelisted by _QUEUE_UPDATE_COLUMNS
        f" VALUES (?,?,?,?,?)"
        f" ON CONFLICT(workshop_id) DO UPDATE SET {', '.join(sets)}",
        params,
    )
    db.commit()


def queue_delete(workshop_id: str) -> None:
    db = get_conn()
    db.execute("DELETE FROM download_queue WHERE workshop_id = ?", (workshop_id,))
    db.commit()


def queue_load_pending() -> list[dict]:
    """Load pending/downloading items from DB (for restart recovery)."""
    db = get_conn()
    rows = db.execute(
        "SELECT * FROM download_queue WHERE status IN ('pending','downloading')"
        " ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def queue_load_all() -> list[dict]:
    """Load all queue items from DB."""
    db = get_conn()
    rows = db.execute("SELECT * FROM download_queue ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def queue_clear_done() -> None:
    """Remove completed/cancelled items from queue table."""
    db = get_conn()
    db.execute("DELETE FROM download_queue WHERE status IN ('done','cancelled')")
    db.commit()
