"""Test database: SQLite CRUD for download history."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.database import (
    clear_history,
    close_db,
    get_download_history,
    get_download_stats,
    init_db,
    record_download,
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
            assert get_conn().execute("PRAGMA user_version").fetchone()[0] == 1
        close_db()
