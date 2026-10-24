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

import logging
import os
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

_log = logging.getLogger(__name__)

# Current schema version. Bump when adding a new migration to _MIGRATIONS.
SCHEMA_VERSION = 4

# Versions whose migration must be preceded by a best-effort copy of the live
# DB (``<db>.bak_v<version>``). Each is a table-rebuild step: a bug in the
# migration would otherwise be able to lose the user's queue.
_BACKUP_BEFORE_VERSIONS: tuple[int, ...] = (3, 4)

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
    (
        2,
        """
        -- v2: persist supportedVersions as a JSON array. NULL means "written by
        -- an older cache row and not collected yet" — cache_db treats it as a
        -- miss and re-parses About.xml once, then fills the column.
        ALTER TABLE local_mod_metadata ADD COLUMN supported_versions TEXT DEFAULT NULL;
        """,
    ),
    (
        3,
        """
        -- v3 (Wave 6a): every mod-scoped row belongs to an instance. A folder
        -- name is only unique *inside* one instance, so both tables need a
        -- composite primary key. SQLite cannot ALTER a PRIMARY KEY, hence the
        -- table rebuild; the transaction wrapper rolls the whole thing back on
        -- any failure.
        ALTER TABLE local_mod_metadata
            ADD COLUMN instance_id TEXT NOT NULL DEFAULT 'default';

        CREATE TABLE local_mod_metadata_v3 (
            instance_id       TEXT    NOT NULL DEFAULT 'default',
            folder            TEXT    NOT NULL,
            name              TEXT    NOT NULL,
            package_id        TEXT    DEFAULT '',
            workshop_id       TEXT    DEFAULT '',
            dir_mtime         REAL    DEFAULT 0,
            cached_at         REAL    DEFAULT 0,
            last_updated      INTEGER DEFAULT 0,
            supported_versions TEXT   DEFAULT NULL,
            PRIMARY KEY (instance_id, folder)
        );
        INSERT INTO local_mod_metadata_v3
            (instance_id, folder, name, package_id, workshop_id,
             dir_mtime, cached_at, last_updated, supported_versions)
        SELECT instance_id, folder, name, package_id, workshop_id,
               dir_mtime, cached_at, COALESCE(last_updated, 0), supported_versions
        FROM local_mod_metadata;
        DROP TABLE local_mod_metadata;
        ALTER TABLE local_mod_metadata_v3 RENAME TO local_mod_metadata;
        CREATE INDEX IF NOT EXISTS idx_lmm_workshop ON local_mod_metadata(workshop_id);
        CREATE INDEX IF NOT EXISTS idx_lmm_instance ON local_mod_metadata(instance_id);

        ALTER TABLE mod_tags ADD COLUMN instance_id TEXT NOT NULL DEFAULT 'default';
        CREATE TABLE mod_tags_v3 (
            instance_id TEXT NOT NULL DEFAULT 'default',
            folder      TEXT NOT NULL,
            tag         TEXT NOT NULL,
            PRIMARY KEY (instance_id, folder, tag)
        );
        INSERT INTO mod_tags_v3 (instance_id, folder, tag)
        SELECT instance_id, folder, tag FROM mod_tags;
        DROP TABLE mod_tags;
        ALTER TABLE mod_tags_v3 RENAME TO mod_tags;
        CREATE INDEX IF NOT EXISTS idx_mt_tag ON mod_tags(instance_id, tag);

        -- Queue/history only gain a column: the queue's instance ownership is
        -- wired up in Wave 6b, and the existing UNIQUE(workshop_id) stays.
        ALTER TABLE download_queue ADD COLUMN instance_id TEXT NOT NULL DEFAULT 'default';
        ALTER TABLE download_history ADD COLUMN instance_id TEXT NOT NULL DEFAULT 'default';
        CREATE INDEX IF NOT EXISTS idx_dq_instance ON download_queue(instance_id);
        CREATE INDEX IF NOT EXISTS idx_dl_instance ON download_history(instance_id);
        """,
    ),
    (
        4,
        """
        -- v4 (Wave 6c): the queue is per-instance. v3 kept UNIQUE(workshop_id),
        -- so a second instance's copy of the same workshop id overwrote the
        -- first one; on restart only the last owner survived. Rebuild both
        -- queue tables with a composite primary key. Existing rows keep their
        -- instance_id (v3 defaulted them to 'default').
        CREATE TABLE download_queue_v4 (
            instance_id TEXT    NOT NULL DEFAULT 'default',
            workshop_id TEXT    NOT NULL,
            name        TEXT    DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'pending',
            progress    REAL    DEFAULT 0.0,
            msg         TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (instance_id, workshop_id)
        );
        INSERT INTO download_queue_v4
            (instance_id, workshop_id, name, status, progress, msg, created_at, updated_at)
        SELECT instance_id, workshop_id, name, status, progress, msg, created_at, updated_at
        FROM download_queue;
        DROP TABLE download_queue;
        ALTER TABLE download_queue_v4 RENAME TO download_queue;
        CREATE INDEX IF NOT EXISTS idx_dq_status ON download_queue(status);
        CREATE INDEX IF NOT EXISTS idx_dq_instance ON download_queue(instance_id);

        -- Scheduler flags follow the same ownership key. The v3 table never
        -- had an instance column, so old flags belong to 'default'.
        CREATE TABLE download_queue_flags_v4 (
            instance_id TEXT    NOT NULL DEFAULT 'default',
            workshop_id TEXT    NOT NULL,
            force       INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (instance_id, workshop_id)
        );
        -- Old flags belong to whichever instance owned the queue row (v3
        -- guaranteed one row per workshop id); orphans fall back to 'default'.
        INSERT INTO download_queue_flags_v4 (instance_id, workshop_id, force, retry_count)
        SELECT COALESCE(q.instance_id, 'default'), f.workshop_id, f.force, f.retry_count
        FROM download_queue_flags f
        LEFT JOIN download_queue q ON q.workshop_id = f.workshop_id;
        DROP TABLE download_queue_flags;
        ALTER TABLE download_queue_flags_v4 RENAME TO download_queue_flags;
        """,
    ),
]

# Per-thread connections: each thread gets its own sqlite3.Connection so
# concurrent FastAPI workers never contend on a single connection.
_local: threading.local = threading.local()
# Guard for the lazily-created initial connection during init_db / close.
_init_lock = threading.Lock()
_initialized = False
# How long get_conn() waits for a concurrent writer before failing.
_BUSY_TIMEOUT_MS = 5000

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


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database to WAL, waiting out a concurrent writer.

    ``PRAGMA journal_mode=WAL`` does NOT go through SQLite's busy handler:
    when another connection holds a write transaction, the pragma returns
    SQLITE_BUSY immediately ("database is locked") regardless of
    busy_timeout. Retry until the same busy timeout elapses so the first
    connection to a not-yet-WAL database waits instead of failing (DATA-2).
    """
    deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def get_conn() -> sqlite3.Connection:
    """Return a per-thread persistent connection, creating it lazily."""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    # close_db() closes every registered connection from the shutting-down
    # thread; other threads still hold a reference to the now-closed object.
    # id() alone is not proof of closure — CPython recycles ids, so a live
    # connection can inherit an id that is still in _closed_conn_ids. Probe
    # the connection instead and treat it as closed only when SQLite agrees.
    if conn is not None and id(conn) in _closed_conn_ids:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            _local.conn = None
            conn = None
        else:
            _closed_conn_ids.discard(id(conn))  # stale id from a recycled object
    if conn is not None:
        return conn
    # NOTE: do NOT call get_conn() while holding _init_lock — the PRAGMA
    # below can block on other connections and _init_lock is non-reentrant,
    # which deadlocks init_db() (it calls get_conn inside the same lock).
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    try:
        _enable_wal(conn)
    except sqlite3.OperationalError:
        conn.close()
        raise
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-8000")  # 8MB page cache
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
    with _init_lock:
        _all_conns.add(conn)
        # A fresh connection may reuse a closed connection's id; it is live,
        # so drop the stale tombstone (DATA-9: the set is pruned on reuse).
        _closed_conn_ids.discard(id(conn))
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
    # A v3 migration rebuilds two tables; if a database already exists on disk
    # it is copied aside first. Remember whether the file pre-existed *before*
    # get_conn() creates an empty one.
    db_existed = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    # Get the connection OUTSIDE the lock: get_conn() itself takes _init_lock
    # (to register in _all_conns) — calling it here inside the lock would
    # deadlock (non-reentrant). PRAGMAs in get_conn can also block on other
    # threads' connections.
    db = get_conn()
    with _init_lock:
        if _initialized:
            return
        _apply_migrations(db, backup_source_existed=db_existed)
        _initialized = True


def _backup_before_migration(db: sqlite3.Connection, version: int) -> None:
    """Copy the live DB to ``<db>.bak_v<version>`` before that table rebuild.

    Never overwrites an existing file: the path may be a backup the user is
    keeping on purpose. Failures are logged and do not block startup — the
    migration transaction is atomic on its own.
    """
    target = DB_PATH.parent / f"{DB_PATH.name}.bak_v{version}"
    if target.exists():
        return
    # Write next to the target, then rename: a crash mid-backup must never
    # leave a truncated backup that the "don't overwrite" check would keep.
    tmp_target = target.with_name(target.name + ".tmp")
    try:
        dest = sqlite3.connect(str(tmp_target))
        try:
            db.backup(dest)
        finally:
            dest.close()
        os.replace(tmp_target, target)
        _log.info("数据库迁移前备份: %s", target)
    except (sqlite3.Error, OSError) as e:  # pragma: no cover — disk/permission failure
        tmp_target.unlink(missing_ok=True)
        _log.warning("数据库迁移前备份失败（继续迁移）: %s", e)


def _apply_migrations(db: sqlite3.Connection, backup_source_existed: bool = False) -> None:
    """Create the base schema, then apply any pending migrations.

    ``PRAGMA user_version`` records the applied schema level; every migration
    in _MIGRATIONS with a higher version is run inside its own transaction.
    A fresh database gets the base DDL then all migrations in order. Table
    rebuilds listed in ``_BACKUP_BEFORE_VERSIONS`` snapshot the live DB first
    (e.g. ``.bak_v4`` holds the pre-migration v3 database).
    """
    current_version = db.execute("PRAGMA user_version").fetchone()[0]
    if backup_source_existed:
        for backup_version in _BACKUP_BEFORE_VERSIONS:
            if current_version < backup_version and any(
                v == backup_version for v, _ in _MIGRATIONS
            ):
                _backup_before_migration(db, backup_version)

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

            -- Scheduler flags for queued items, kept in their own table so the
            -- base schema is additive for existing databases (CREATE TABLE IF
            -- NOT EXISTS runs on every init_db, no user_version bump needed).
            --   force       -- update intent: replace an installed copy
            --   retry_count -- interrupted-download recoveries still unresolved
            CREATE TABLE IF NOT EXISTS download_queue_flags (
                workshop_id TEXT PRIMARY KEY,
                force       INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0
            );
        """
    )
    db.commit()

    current = db.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in _MIGRATIONS:
        if version <= current:
            continue
        # execute() each statement instead of executescript(): executescript()
        # commits the open transaction before running, which made a half-applied
        # migration impossible to roll back (DATA-1).
        db.execute("BEGIN")
        try:
            _execute_migration_sql(db, sql)
            db.execute(f"PRAGMA user_version = {version}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        current = version


def _execute_migration_sql(db: sqlite3.Connection, sql: str) -> None:
    """Execute a migration script statement-by-statement inside the caller's
    transaction. Comment-only chunks are skipped."""
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if _contains_sql(statement):
                db.execute(statement)
            statement = ""
    if statement.strip() and _contains_sql(statement):
        db.execute(statement)


def _contains_sql(text: str) -> bool:
    return any(line.strip() and not line.strip().startswith("--") for line in text.splitlines())


def _resolve_instance(instance_id: str | None) -> str:
    """Explicit id, else the currently active instance (default scope)."""
    if instance_id:
        return instance_id
    # Imported lazily: config imports steam_db, which must not import the DB
    # layer at module load time.
    from rwmod.config import current_instance_id

    return current_instance_id()


def record_download(
    workshop_id: str,
    status: str,
    mod_name: str = "",
    package_id: str = "",
    msg: str = "",
    instance_id: str | None = None,
) -> int:
    db = get_conn()
    sql = (
        "INSERT INTO download_history"
        " (instance_id, workshop_id, mod_name, package_id, status, msg)"
        " VALUES (?,?,?,?,?,?)"
    )
    cur = db.execute(
        sql, (_resolve_instance(instance_id), workshop_id, mod_name, package_id, status, msg)
    )
    db.commit()
    return cur.lastrowid or -1


def get_download_history(
    limit: int = 50, status: str = "", instance_id: str | None = None
) -> list[dict]:
    db = get_conn()
    query = "SELECT * FROM download_history WHERE instance_id = ?"
    params: list = [_resolve_instance(instance_id)]
    if status:
        query += " AND status = ?"
        params.append(status)
    # id is the tie-breaker: datetime('now') has one-second resolution, and
    # without it two downloads in the same second come back in an arbitrary
    # order (the old index-scan accident was not a guarantee).
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
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


def get_download_stats(instance_id: str | None = None) -> dict:
    db = get_conn()
    iid = _resolve_instance(instance_id)
    total = db.execute(
        "SELECT COUNT(*) as n FROM download_history WHERE instance_id = ?", (iid,)
    ).fetchone()["n"]
    success = db.execute(
        "SELECT COUNT(*) as n FROM download_history WHERE instance_id = ? AND status='success'",
        (iid,),
    ).fetchone()["n"]
    failed = db.execute(
        "SELECT COUNT(*) as n FROM download_history WHERE instance_id = ? AND status='failed'",
        (iid,),
    ).fetchone()["n"]
    return {"total": total, "success": success, "failed": failed}


def clear_history(instance_id: str | None = None) -> None:
    db = get_conn()
    db.execute(
        "DELETE FROM download_history WHERE instance_id = ?", (_resolve_instance(instance_id),)
    )
    db.commit()


# ── local mod workshop_id index (folder → workshop_id) ──────────────
# Used by downloader._find_existing to avoid a full directory scan on every
# download. The index lives in local_mod_metadata.workshop_id; folder names
# are the primary key. After a download we upsert the workshop_id so subsequent
# lookups are O(1) SQLite queries instead of O(n) directory scans.


def find_local_mods_by_workshop_id(workshop_id: str, instance_id: str | None = None) -> list[str]:
    """Return folder names whose PublishedFileId matches workshop_id."""
    db = get_conn()
    rows = db.execute(
        "SELECT folder FROM local_mod_metadata WHERE instance_id = ? AND workshop_id = ?",
        (_resolve_instance(instance_id), workshop_id),
    ).fetchall()
    return [r["folder"] for r in rows]


def find_local_mods_by_workshop_ids(
    workshop_ids: list[str], instance_id: str | None = None
) -> dict[str, list[str]]:
    """Batch variant — one query for many workshop ids.

    Returns {workshop_id: [folder, ...]} for the ids that have a metadata
    row. Used by the batch downloader to avoid N separate queries (and N
    fallback directory scans) when checking which mods are already installed.
    """
    if not workshop_ids:
        return {}
    db = get_conn()
    marks = ",".join("?" for _ in workshop_ids)
    params = [_resolve_instance(instance_id), *workshop_ids]
    rows = db.execute(
        f"SELECT folder, workshop_id FROM local_mod_metadata"  # nosec B608 — marks is '?' placeholders only, values bound
        f" WHERE instance_id = ? AND workshop_id IN ({marks})",
        params,
    ).fetchall()
    result: dict[str, list[str]] = {}
    for r in rows:
        result.setdefault(r["workshop_id"], []).append(r["folder"])
    return result


def upsert_local_mod_workshop_id(
    folder: str, workshop_id: str, instance_id: str | None = None
) -> None:
    """Record (folder → workshop_id) mapping in the metadata cache index."""
    db = get_conn()
    db.execute(
        """INSERT INTO local_mod_metadata
               (instance_id, folder, name, workshop_id, dir_mtime, cached_at)
           VALUES (?, ?, '', ?, 0, ?)
           ON CONFLICT(instance_id, folder) DO UPDATE SET workshop_id = excluded.workshop_id""",
        (_resolve_instance(instance_id), folder, workshop_id, time.time()),
    )
    db.commit()


def remove_local_mod_workshop_id(folder: str, instance_id: str | None = None) -> None:
    """Remove a folder mapping (e.g. after force-reinstall deletes a folder)."""
    db = get_conn()
    db.execute(
        "DELETE FROM local_mod_metadata WHERE instance_id = ? AND folder = ?",
        (_resolve_instance(instance_id), folder),
    )
    db.commit()


# ── last-updated timestamp (migrated from per-mod .rwmod_last_updated) ──


def get_last_updated(folder: str, instance_id: str | None = None) -> int:
    """Return the stored update-check timestamp for a mod folder (0 if unset)."""
    db = get_conn()
    row = db.execute(
        "SELECT last_updated FROM local_mod_metadata WHERE instance_id = ? AND folder = ?",
        (_resolve_instance(instance_id), folder),
    ).fetchone()
    if row is None or row["last_updated"] is None:
        return 0
    return int(row["last_updated"])


def set_last_updated(folder: str, timestamp: int, instance_id: str | None = None) -> None:
    """Record when a mod folder was last compared against Steam Workshop.

    Upserts so a freshly downloaded mod (no metadata row yet) gets its
    timestamp recorded too.
    """
    db = get_conn()
    db.execute(
        """INSERT INTO local_mod_metadata (instance_id, folder, name, last_updated)
           VALUES (?, ?, '', ?)
           ON CONFLICT(instance_id, folder) DO UPDATE SET
             last_updated = excluded.last_updated""",
        (_resolve_instance(instance_id), folder, timestamp),
    )
    db.commit()


# ── queue persistence ──────────────────────────────────────────────


# Whitelist of columns queue_upsert may update — drops unknown keys so caller
# input can never reach the SQL as identifiers (B608).
_QUEUE_UPDATE_COLUMNS = frozenset({"name", "status", "progress", "msg"})
# Scheduling flags live in download_queue_flags (kept apart from the status
# columns so old databases need no ALTER TABLE migration).
_QUEUE_FLAG_COLUMNS = frozenset({"force", "retry_count"})


def queue_upsert(workshop_id: str, instance_id: str | None = None, **kwargs: object) -> None:
    """Insert or update a queue item owned by *instance_id*.

    ``instance_id`` defaults to the active instance. Since schema v4 the row
    key is ``(instance_id, workshop_id)``, so two installations can queue the
    same workshop id without one overwriting the other.

    kwargs: name, status, progress, msg (download_queue columns) plus force,
    retry_count (scheduler flags in download_queue_flags, same composite key).
    """
    db = get_conn()
    iid = _resolve_instance(instance_id)
    safe = {k: v for k, v in kwargs.items() if k in _QUEUE_UPDATE_COLUMNS}
    flags = {k: v for k, v in kwargs.items() if k in _QUEUE_FLAG_COLUMNS}
    name = safe.get("name", "")
    status = safe.get("status", "pending")
    progress = safe.get("progress", 0.0)
    msg = safe.get("msg", "")

    sets = [f"{k} = ?" for k in safe]
    params: list = [iid, workshop_id, name, status, progress, msg]
    params.extend(safe[k] for k in safe)

    sets.append("updated_at = datetime('now')")
    db.execute(
        f"INSERT INTO download_queue (instance_id, workshop_id, name, status, progress, msg)"  # nosec B608 — keys whitelisted by _QUEUE_UPDATE_COLUMNS
        f" VALUES (?,?,?,?,?,?)"
        f" ON CONFLICT(instance_id, workshop_id) DO UPDATE SET {', '.join(sets)}",
        params,
    )
    if flags:
        raw_retry = flags.get("retry_count")
        retry_count = int(raw_retry) if isinstance(raw_retry, (int, float, str)) else 0
        db.execute(
            "INSERT INTO download_queue_flags (instance_id, workshop_id, force, retry_count)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(instance_id, workshop_id) DO UPDATE SET"
            " force = excluded.force, retry_count = excluded.retry_count",
            (iid, workshop_id, 1 if flags.get("force") else 0, retry_count),
        )
    db.commit()


def queue_delete(workshop_id: str, instance_id: str | None = None) -> None:
    """Delete a queue row (and its orphaned flags).

    ``instance_id=None`` keeps the old unscoped meaning (delete the row no
    matter who owns it). An explicit id only deletes a row owned by it.
    """
    db = get_conn()
    if instance_id is None:
        db.execute("DELETE FROM download_queue WHERE workshop_id = ?", (workshop_id,))
        db.execute("DELETE FROM download_queue_flags WHERE workshop_id = ?", (workshop_id,))
    else:
        db.execute(
            "DELETE FROM download_queue WHERE instance_id = ? AND workshop_id = ?",
            (instance_id, workshop_id),
        )
        db.execute(
            "DELETE FROM download_queue_flags WHERE instance_id = ? AND workshop_id = ?",
            (instance_id, workshop_id),
        )
    db.commit()


_QUEUE_SELECT = (
    "SELECT q.*, COALESCE(f.force, 0) AS force, COALESCE(f.retry_count, 0) AS retry_count"
    " FROM download_queue q"
    " LEFT JOIN download_queue_flags f"
    " ON f.instance_id = q.instance_id AND f.workshop_id = q.workshop_id"
)


def queue_load_pending(instance_id: str | None = None) -> list[dict]:
    """Load pending/downloading items from DB (for restart recovery).

    ``instance_id=None`` returns every instance's rows (recovery and the
    instance-switch 409 guard need the whole queue); an explicit id filters.
    """
    db = get_conn()
    query = _QUEUE_SELECT + " WHERE q.status IN ('pending','downloading')"
    params: list = []
    if instance_id is not None:
        query += " AND q.instance_id = ?"
        params.append(instance_id)
    query += " ORDER BY q.created_at ASC"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def queue_load_all(instance_id: str | None = None) -> list[dict]:
    """Load all queue items from DB (optionally only one instance's)."""
    db = get_conn()
    query = _QUEUE_SELECT
    params: list = []
    if instance_id is not None:
        query += " WHERE q.instance_id = ?"
        params.append(instance_id)
    query += " ORDER BY q.created_at ASC"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def queue_clear_done(instance_id: str | None = None) -> None:
    """Remove completed/cancelled items and any orphaned scheduler flags.

    ``instance_id=None`` clears every instance's finished rows (queue-wide
    "clear" button); an explicit id leaves other instances untouched.
    """
    db = get_conn()
    if instance_id is None:
        db.execute("DELETE FROM download_queue WHERE status IN ('done','cancelled')")
    else:
        db.execute(
            "DELETE FROM download_queue WHERE instance_id = ? AND status IN ('done','cancelled')",
            (instance_id,),
        )
    db.execute(
        "DELETE FROM download_queue_flags"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM download_queue q"
        "   WHERE q.instance_id = download_queue_flags.instance_id"
        "     AND q.workshop_id = download_queue_flags.workshop_id"
        " )"
    )
    db.commit()
