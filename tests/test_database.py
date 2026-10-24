"""Test database: SQLite CRUD for download history."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.database import (
    clear_history,
    close_db,
    find_local_mods_by_workshop_id,
    get_download_history,
    get_download_stats,
    get_last_updated,
    init_db,
    record_download,
    remove_local_mod_workshop_id,
    set_last_updated,
    upsert_local_mod_workshop_id,
)


@pytest.fixture
def temp_db(tmp_path: Path):
    """Use temp SQLite DB instead of production DB."""
    db_path = tmp_path / "test.db"
    with patch("rwmod.database.DB_PATH", db_path):
        init_db()
        yield
        close_db()  # close before unlink — WAL mode holds file lock on Windows
    db_path.unlink(missing_ok=True)


class TestHistory:
    def test_record_and_retrieve(self, temp_db):
        record_download("123", "success", mod_name="Test Mod")
        record_download("456", "failed", msg="SteamCMD error")
        history = get_download_history()
        assert len(history) == 2
        assert history[0]["status"] == "failed"  # newest first
        assert history[1]["status"] == "success"
        assert history[1]["mod_name"] == "Test Mod"

    def test_filter_by_status(self, temp_db):
        record_download("111", "success")
        record_download("222", "failed")
        record_download("333", "success")
        assert len(get_download_history(status="success")) == 2
        assert len(get_download_history(status="failed")) == 1

    def test_stats(self, temp_db):
        record_download("a", "success")
        record_download("b", "success")
        record_download("c", "failed")
        stats = get_download_stats()
        assert stats["total"] == 3
        assert stats["success"] == 2
        assert stats["failed"] == 1

    def test_clear(self, temp_db):
        record_download("1", "success")
        record_download("2", "success")
        clear_history()
        assert get_download_history() == []
        assert get_download_stats()["total"] == 0

    def test_limit(self, temp_db):
        for i in range(10):
            record_download(str(i), "success")
        assert len(get_download_history(limit=3)) == 3


class TestSchemaMigration:
    """init_db() must create the schema and migrate legacy DBs in place."""

    def test_fresh_db_has_last_updated_column(self, tmp_path: Path):
        db_path = tmp_path / "fresh.db"
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            from rwmod.database import SCHEMA_VERSION, get_conn

            cols = {r["name"] for r in get_conn().execute("PRAGMA table_info(local_mod_metadata)")}
            assert "last_updated" in cols
            assert get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        close_db()

    def test_legacy_db_upgrades_preserving_data(self, tmp_path: Path):
        """A pre-migration DB (v0, no last_updated) upgrades in place."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE local_mod_metadata (
                folder       TEXT PRIMARY KEY,
                name         TEXT    NOT NULL,
                package_id   TEXT    DEFAULT '',
                workshop_id  TEXT    DEFAULT '',
                dir_mtime    REAL    DEFAULT 0,
                cached_at    REAL    DEFAULT 0
            );
            INSERT INTO local_mod_metadata (folder, name, package_id, workshop_id)
            VALUES ('MyMod', 'My Mod', 'author.mod', '12345');
            """
        )
        conn.commit()
        conn.close()

        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            from rwmod.database import get_conn

            # Data survives the ALTER TABLE.
            row = (
                get_conn()
                .execute("SELECT folder, name, workshop_id, last_updated FROM local_mod_metadata")
                .fetchone()
            )
            assert row["folder"] == "MyMod"
            assert row["name"] == "My Mod"
            assert row["workshop_id"] == "12345"
            assert row["last_updated"] == 0
        close_db()


def _make_legacy_v2_db(path: Path) -> None:
    """A v2-era database: single-column folder PKs, no instance_id anywhere."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA user_version = 2;
        CREATE TABLE local_mod_metadata (
            folder       TEXT PRIMARY KEY,
            name         TEXT    NOT NULL,
            package_id   TEXT    DEFAULT '',
            workshop_id  TEXT    DEFAULT '',
            dir_mtime    REAL    DEFAULT 0,
            cached_at    REAL    DEFAULT 0,
            last_updated INTEGER DEFAULT 0,
            supported_versions TEXT DEFAULT NULL
        );
        CREATE TABLE mod_tags (
            folder TEXT NOT NULL,
            tag    TEXT NOT NULL,
            PRIMARY KEY (folder, tag)
        );
        CREATE TABLE download_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            workshop_id TEXT    NOT NULL UNIQUE,
            name        TEXT    DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'pending',
            progress    REAL    DEFAULT 0.0,
            msg         TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE download_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            workshop_id TEXT    NOT NULL,
            mod_name    TEXT    DEFAULT '',
            package_id  TEXT    DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'pending',
            msg         TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_lmm_workshop ON local_mod_metadata(workshop_id);
        INSERT INTO local_mod_metadata (folder, name, package_id, workshop_id, last_updated)
        VALUES ('MyMod', 'My Mod', 'author.mod', '12345', 777);
        INSERT INTO mod_tags (folder, tag) VALUES ('MyMod', 'QoL');
        INSERT INTO download_queue (workshop_id, name, status)
        VALUES ('999', 'Queue Mod', 'done');
        INSERT INTO download_history (workshop_id, mod_name, status)
        VALUES ('888', 'Old Mod', 'success');
        """
    )
    conn.commit()
    conn.close()


def _table_pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608 — literal table name
    return [r[1] for r in sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608


class TestInstanceMigrationV3:
    """Schema v3: instance-scoped metadata/tags, instance columns on history/queue."""

    def test_fresh_db_is_v4_with_composite_primary_keys(self, tmp_path: Path):
        db_path = tmp_path / "fresh_v4.db"
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            from rwmod.database import SCHEMA_VERSION, get_conn

            conn = get_conn()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
            assert _table_pk_columns(conn, "local_mod_metadata") == ["instance_id", "folder"]
            assert _table_pk_columns(conn, "mod_tags") == ["instance_id", "folder", "tag"]
            # Wave 6c: both queue tables are keyed by (instance, workshop id).
            assert _table_pk_columns(conn, "download_queue") == ["instance_id", "workshop_id"]
            assert _table_pk_columns(conn, "download_queue_flags") == [
                "instance_id",
                "workshop_id",
            ]
            for table in ("download_queue", "download_history", "download_queue_flags"):
                assert "instance_id" in _table_columns(conn, table)
        close_db()

    def test_legacy_v2_db_upgrades_and_rows_default_to_default_instance(self, tmp_path: Path):
        db_path = tmp_path / "legacy_v2.db"
        _make_legacy_v2_db(db_path)

        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            from rwmod.database import get_conn

            conn = get_conn()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
            row = conn.execute(
                "SELECT instance_id, folder, name, workshop_id, last_updated"
                " FROM local_mod_metadata"
            ).fetchone()
            assert tuple(row) == ("default", "MyMod", "My Mod", "12345", 777)
            tag = conn.execute("SELECT instance_id, folder, tag FROM mod_tags").fetchone()
            assert tuple(tag) == ("default", "MyMod", "QoL")
            assert conn.execute("SELECT instance_id FROM download_queue").fetchone()[0] == "default"
            assert (
                conn.execute("SELECT instance_id FROM download_history").fetchone()[0] == "default"
            )
        close_db()

    def test_migration_is_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "idem.db"
        _make_legacy_v2_db(db_path)
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            close_db()
            init_db()
            init_db()
            from rwmod.database import get_conn

            conn = get_conn()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
            assert conn.execute("SELECT COUNT(*) FROM local_mod_metadata").fetchone()[0] == 1
        close_db()

    def test_migration_backs_up_a_preexisting_db(self, tmp_path: Path):
        db_path = tmp_path / ".rwmod.db"
        _make_legacy_v2_db(db_path)
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            # Each rebuild step takes its own pre-migration copy. Both are
            # snapshots of the v2 database here (the backup runs before any
            # migration is applied).
            for suffix in ("bak_v3", "bak_v4"):
                backup = tmp_path / f".rwmod.db.{suffix}"
                assert backup.exists(), suffix
                pre = sqlite3.connect(backup)
                try:
                    assert pre.execute("PRAGMA user_version").fetchone()[0] == 2
                    assert pre.execute("SELECT COUNT(*) FROM local_mod_metadata").fetchone()[0] == 1
                finally:
                    pre.close()
            # A fresh database has nothing to back up.
            close_db()
        fresh = tmp_path / "fresh.db"
        with patch("rwmod.database.DB_PATH", fresh):
            init_db()
            assert not (tmp_path / "fresh.db.bak_v3").exists()
            assert not (tmp_path / "fresh.db.bak_v4").exists()
        close_db()

    def test_backup_never_overwrites_a_file_the_user_owns(self, tmp_path: Path):
        db_path = tmp_path / ".rwmod.db"
        _make_legacy_v2_db(db_path)
        owned_v3 = tmp_path / ".rwmod.db.bak_v3"
        owned_v4 = tmp_path / ".rwmod.db.bak_v4"
        owned_v3.write_text("do not touch", encoding="utf-8")
        owned_v4.write_text("do not touch either", encoding="utf-8")
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
        assert owned_v3.read_text(encoding="utf-8") == "do not touch"
        assert owned_v4.read_text(encoding="utf-8") == "do not touch either"
        close_db()

    def test_failed_v3_migration_rolls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_path = tmp_path / "rollback.db"
        _make_legacy_v2_db(db_path)
        import rwmod.database as db_mod

        real = db_mod._execute_migration_sql

        def boom(conn: sqlite3.Connection, sql: str) -> None:
            if "mod_tags_v3" in sql:
                # Half-apply the migration, then fail: the transaction must undo it.
                conn.execute(
                    "ALTER TABLE local_mod_metadata"
                    " ADD COLUMN instance_id TEXT NOT NULL DEFAULT 'default'"
                )
                raise RuntimeError("boom")
            real(conn, sql)

        monkeypatch.setattr(db_mod, "_execute_migration_sql", boom)
        with patch("rwmod.database.DB_PATH", db_path):
            with pytest.raises(RuntimeError):
                init_db()
            conn = db_mod.get_conn()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
            assert "instance_id" not in _table_columns(conn, "local_mod_metadata")
            assert _table_pk_columns(conn, "local_mod_metadata") == ["folder"]
        close_db()

    def test_real_database_readonly_copy_migrates(self, tmp_path: Path):
        """Migrate a *copy* of the developer's real DB; the original is never opened
        for writing (only copied, and its size/mtime are asserted unchanged)."""
        real_home = Path(os.path.expanduser("~"))
        real_db = real_home / ".rwmod.db"
        if not real_db.exists():
            pytest.skip("real ~/.rwmod.db not available")

        before = real_db.stat()
        copy = tmp_path / "real_copy.db"
        shutil.copy2(real_db, copy)
        for suffix in ("-wal", "-shm"):
            src = Path(str(real_db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(copy) + suffix))

        pre_conn = sqlite3.connect(copy)
        try:
            pre_version = pre_conn.execute("PRAGMA user_version").fetchone()[0]
            pre_counts = {
                table: pre_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608 — literal names
                for table in ("local_mod_metadata", "download_queue", "download_history")
            }
        finally:
            pre_conn.close()

        with patch("rwmod.database.DB_PATH", copy):
            init_db()
            from rwmod.database import SCHEMA_VERSION, get_conn

            conn = get_conn()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
            assert _table_pk_columns(conn, "local_mod_metadata") == ["instance_id", "folder"]
            assert _table_pk_columns(conn, "mod_tags") == ["instance_id", "folder", "tag"]
            # Wave 6c: the v4 rebuild preserves the row set and switches both
            # queue tables to the composite key.
            assert _table_pk_columns(conn, "download_queue") == ["instance_id", "workshop_id"]
            assert _table_pk_columns(conn, "download_queue_flags") == [
                "instance_id",
                "workshop_id",
            ]
            for table, expected in pre_counts.items():
                actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608 — literal names
                assert actual == expected, f"{table}: {actual} != {expected}"
            bad = conn.execute(
                "SELECT COUNT(*) FROM local_mod_metadata WHERE instance_id != 'default'"
            ).fetchone()[0]
            assert bad == 0
        close_db()

        if pre_version < 3:
            assert (tmp_path / "real_copy.db.bak_v3").exists()
        if pre_version < 4:
            backup = tmp_path / "real_copy.db.bak_v4"
            assert backup.exists(), "v4 must snapshot the pre-migration database"
            backup_conn = sqlite3.connect(backup)
            try:
                # The backup is the v3 database: old schema, same rows.
                assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == pre_version
                assert (
                    backup_conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0]
                    == pre_counts["download_queue"]
                )
            finally:
                backup_conn.close()

        after = real_db.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns), (
            "the real ~/.rwmod.db was modified by the migration test"
        )


class TestInstanceScopedMetadata:
    """Two instances may contain a folder with the same name without colliding."""

    def test_same_folder_does_not_collide(self, temp_db):
        upsert_local_mod_workshop_id("Shared", "111", instance_id="alpha")
        upsert_local_mod_workshop_id("Shared", "222", instance_id="beta")

        assert find_local_mods_by_workshop_id("111", instance_id="alpha") == ["Shared"]
        assert find_local_mods_by_workshop_id("111", instance_id="beta") == []
        assert find_local_mods_by_workshop_id("222", instance_id="beta") == ["Shared"]

        set_last_updated("Shared", 10, instance_id="alpha")
        set_last_updated("Shared", 20, instance_id="beta")
        assert get_last_updated("Shared", instance_id="alpha") == 10
        assert get_last_updated("Shared", instance_id="beta") == 20

        remove_local_mod_workshop_id("Shared", instance_id="alpha")
        assert find_local_mods_by_workshop_id("111", instance_id="alpha") == []
        assert find_local_mods_by_workshop_id("222", instance_id="beta") == ["Shared"]

    def test_default_instance_is_used_when_omitted(self, temp_db):
        upsert_local_mod_workshop_id("Unscoped", "333")
        assert find_local_mods_by_workshop_id("333") == ["Unscoped"]

    def test_download_history_is_scoped(self, temp_db):
        record_download("1", "success", instance_id="alpha")
        record_download("2", "failed", instance_id="beta")
        assert [r["workshop_id"] for r in get_download_history(instance_id="alpha")] == ["1"]
        assert [r["workshop_id"] for r in get_download_history(instance_id="beta")] == ["2"]
        assert get_download_stats(instance_id="alpha")["total"] == 1

    def test_cache_db_rows_are_instance_scoped(self, temp_db, tmp_path: Path):
        from rwmod.cache_db import get_or_refresh_metas

        dir_a = tmp_path / "a" / "Mods"
        dir_b = tmp_path / "b" / "Mods"
        for d, name, pkg in ((dir_a, "Alpha", "a.alpha"), (dir_b, "Beta", "b.beta")):
            about = d / "Same" / "About"
            about.mkdir(parents=True)
            (about / "About.xml").write_text(
                f"<ModMetaData><name>{name}</name><packageId>{pkg}</packageId></ModMetaData>"
            )

        assert [m.name for m in get_or_refresh_metas(dir_a, instance_id="alpha")] == ["Alpha"]
        assert [m.name for m in get_or_refresh_metas(dir_b, instance_id="beta")] == ["Beta"]

        from rwmod.database import get_conn

        rows = (
            get_conn()
            .execute(
                "SELECT instance_id, folder, name FROM local_mod_metadata ORDER BY instance_id"
            )
            .fetchall()
        )
        assert [tuple(r) for r in rows] == [
            ("alpha", "Same", "Alpha"),
            ("beta", "Same", "Beta"),
        ]

        # A rescan of one instance must not clear the other's rows.
        get_or_refresh_metas(dir_a, instance_id="alpha")
        assert get_conn().execute("SELECT COUNT(*) FROM local_mod_metadata").fetchone()[0] == 2


class TestLastUpdated:
    def test_roundtrip(self, temp_db):
        from rwmod.database import get_last_updated, set_last_updated

        assert get_last_updated("MyMod") == 0
        set_last_updated("MyMod", 12345)
        assert get_last_updated("MyMod") == 12345

    def test_upsert_new_folder(self, temp_db):
        """set_last_updated must work for a folder with no metadata row yet
        (freshly downloaded mod)."""
        from rwmod.database import get_last_updated, set_last_updated

        set_last_updated("BrandNew", 999)
        assert get_last_updated("BrandNew") == 999

    def test_refresh_does_not_wipe_last_updated(self, tmp_path: Path):
        """The metadata-cache upsert must preserve last_updated (the old
        INSERT OR REPLACE deleted the row and zeroed it)."""
        db_path = tmp_path / "m.db"
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            from rwmod.cache_db import get_or_refresh_metas
            from rwmod.database import get_conn, get_last_updated, set_last_updated

            mods_dir = tmp_path / "Mods"
            mod = mods_dir / "MyMod"
            about = mod / "About"
            about.mkdir(parents=True)
            (about / "About.xml").write_text(
                "<ModMetaData><name>My Mod</name><packageId>author.mod</packageId></ModMetaData>"
            )
            get_or_refresh_metas(mods_dir)
            set_last_updated("MyMod", 777)

            # Re-scan (mtime changed → upsert path) must keep the timestamp.
            import os

            os.utime(mod, (mod.stat().st_atime, mod.stat().st_mtime + 1))
            get_or_refresh_metas(mods_dir)
            assert get_last_updated("MyMod") == 777
            from rwmod.database import SCHEMA_VERSION

            assert get_conn().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        close_db()


class TestQueueInstanceScope:
    """Wave 6b: queue rows carry the instance that owns them."""

    def test_queue_functions_accept_and_filter_instance_id(self, temp_db):
        from rwmod.database import (
            queue_clear_done,
            queue_delete,
            queue_load_all,
            queue_load_pending,
            queue_upsert,
        )

        queue_upsert("1", instance_id="alpha", name="A", status="done")
        queue_upsert("2", instance_id="alpha", name="B", status="pending")
        queue_upsert("3", instance_id="beta", name="C", status="pending")

        assert {r["workshop_id"] for r in queue_load_all(instance_id="alpha")} == {"1", "2"}
        assert {r["workshop_id"] for r in queue_load_pending(instance_id="beta")} == {"3"}
        assert {r["instance_id"] for r in queue_load_all()} == {"alpha", "beta"}

        queue_clear_done(instance_id="alpha")
        assert {r["workshop_id"] for r in queue_load_all()} == {"2", "3"}

        queue_delete("2", instance_id="beta")  # other instance → no-op
        assert {r["workshop_id"] for r in queue_load_all()} == {"2", "3"}
        queue_delete("2", instance_id="alpha")
        assert {r["workshop_id"] for r in queue_load_all()} == {"3"}

    def test_same_workshop_id_coexists_per_instance(self, temp_db):
        """Wave 6c: the composite key keeps both instances' rows."""
        from rwmod.database import queue_load_all, queue_upsert

        queue_upsert("1", instance_id="alpha", name="A", status="pending")
        queue_upsert("1", instance_id="beta", name="B", status="done")

        rows = queue_load_all()
        assert len(rows) == 2
        assert {(r["instance_id"], r["workshop_id"]) for r in rows} == {
            ("alpha", "1"),
            ("beta", "1"),
        }
        assert {r["instance_id"]: r["name"] for r in rows} == {"alpha": "A", "beta": "B"}

    def test_flags_are_scoped_by_instance(self, temp_db):
        """download_queue_flags is keyed by (instance_id, workshop_id) too."""
        from rwmod.database import queue_load_all, queue_upsert

        queue_upsert("1", instance_id="alpha", status="pending", force=True, retry_count=2)
        queue_upsert("1", instance_id="beta", status="pending", force=False, retry_count=0)

        rows = {r["instance_id"]: r for r in queue_load_all()}
        assert (rows["alpha"]["force"], rows["alpha"]["retry_count"]) == (1, 2)
        assert (rows["beta"]["force"], rows["beta"]["retry_count"]) == (0, 0)


def _make_v3_db(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a genuine v3 database by running only migrations 1..3.

    Uses the production migration code (not a hand-written schema) so the
    v3→v4 test exercises the real upgrade path.
    """
    import rwmod.database as db_mod

    with monkeypatch.context() as m:
        m.setattr(db_mod, "_MIGRATIONS", [x for x in db_mod._MIGRATIONS if x[0] <= 3])
        m.setattr(db_mod, "_BACKUP_BEFORE_VERSIONS", (3,))
        close_db()
        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            conn = db_mod.get_conn()
            conn.execute(
                "INSERT INTO download_queue (instance_id, workshop_id, name, status)"
                " VALUES ('default','999','Queue Mod','done')"
            )
            conn.execute(
                "INSERT INTO download_queue (instance_id, workshop_id, name, status)"
                " VALUES ('alpha','888','Alpha Mod','pending')"
            )
            conn.execute(
                "INSERT INTO download_queue_flags (workshop_id, force, retry_count)"
                " VALUES ('999',1,2)"
            )
            conn.execute(
                "INSERT INTO download_queue_flags (workshop_id, force, retry_count)"
                " VALUES ('888',0,1)"
            )
            conn.commit()
            close_db()
    return db_path


class TestQueueCompositeKeyV4:
    """Wave 6c — the queue tables are keyed by (instance_id, workshop_id)."""

    def test_v3_database_rebuilds_with_composite_keys(self, tmp_path: Path, monkeypatch):
        db_path = _make_v3_db(tmp_path / "v3_to_v4.db", monkeypatch)

        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
            from rwmod.database import SCHEMA_VERSION, get_conn

            conn = get_conn()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
            assert _table_pk_columns(conn, "download_queue") == ["instance_id", "workshop_id"]
            assert _table_pk_columns(conn, "download_queue_flags") == [
                "instance_id",
                "workshop_id",
            ]

            rows = conn.execute(
                "SELECT instance_id, workshop_id, name, status FROM download_queue"
                " ORDER BY instance_id"
            ).fetchall()
            assert [tuple(r) for r in rows] == [
                ("alpha", "888", "Alpha Mod", "pending"),
                ("default", "999", "Queue Mod", "done"),
            ]
            # Pre-v4 flags had no instance column; old rows become 'default'.
            flags = conn.execute(
                "SELECT instance_id, workshop_id, force, retry_count"
                " FROM download_queue_flags ORDER BY instance_id"
            ).fetchall()
            assert [tuple(r) for r in flags] == [
                ("alpha", "888", 0, 1),
                ("default", "999", 1, 2),
            ]
            # The same workshop id may now be owned by two instances.
            conn.execute(
                "INSERT INTO download_queue (instance_id, workshop_id, name, status)"
                " VALUES ('beta','999','Beta copy','pending')"
            )
            conn.commit()
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM download_queue WHERE workshop_id='999'"
                ).fetchone()[0]
                == 2
            )
        close_db()

    def test_v4_backup_is_the_v3_database(self, tmp_path: Path, monkeypatch):
        db_path = _make_v3_db(tmp_path / ".rwmod.db", monkeypatch)

        with patch("rwmod.database.DB_PATH", db_path):
            init_db()
        close_db()

        backup = tmp_path / ".rwmod.db.bak_v4"
        assert backup.exists()
        backup_conn = sqlite3.connect(backup)
        try:
            assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert backup_conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 2
            # v3 shape: flags had no instance_id, the queue still had its id PK.
            assert "instance_id" not in _table_columns(backup_conn, "download_queue_flags")
            assert _table_pk_columns(backup_conn, "download_queue") == ["id"]
        finally:
            backup_conn.close()

    def test_failed_v4_migration_rolls_back(self, tmp_path: Path, monkeypatch):
        db_path = _make_v3_db(tmp_path / "v4_rollback.db", monkeypatch)
        import rwmod.database as db_mod

        real = db_mod._execute_migration_sql

        def boom(conn: sqlite3.Connection, sql: str) -> None:
            if "download_queue_v4" in sql:
                conn.execute("CREATE TABLE half_v4 (x INTEGER)")
                raise RuntimeError("boom")
            real(conn, sql)

        monkeypatch.setattr(db_mod, "_execute_migration_sql", boom)
        with patch("rwmod.database.DB_PATH", db_path):
            with pytest.raises(RuntimeError):
                init_db()
            conn = db_mod.get_conn()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert _table_pk_columns(conn, "download_queue") == ["id"]
            assert conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 2
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "half_v4" not in tables, "partial v4 schema survived the rollback"
        close_db()
