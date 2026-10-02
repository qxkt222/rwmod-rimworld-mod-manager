"""Test queue persistence: SQLite save/load, restart recovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.database import (
    close_db,
    get_conn,
    init_db,
    queue_clear_done,
    queue_delete,
    queue_load_all,
    queue_load_pending,
    queue_upsert,
)


@pytest.fixture
def temp_db(tmp_path: Path):
    """Use temp SQLite DB with queue table."""
    db_path = tmp_path / "test_queue.db"
    with patch("rwmod.database.DB_PATH", db_path):
        init_db()
        yield
        close_db()
    db_path.unlink(missing_ok=True)


class TestQueueUpsert:
    def test_insert_new(self, temp_db):
        queue_upsert("123", name="Test Mod", status="pending")
        items = queue_load_all()
        assert len(items) == 1
        assert items[0]["workshop_id"] == "123"
        assert items[0]["name"] == "Test Mod"
        assert items[0]["status"] == "pending"

    def test_update_existing(self, temp_db):
        queue_upsert("123", name="Old Name", status="pending")
        queue_upsert("123", name="Updated", status="downloading", progress=0.5)
        items = queue_load_all()
        assert len(items) == 1
        assert items[0]["name"] == "Updated"
        assert items[0]["status"] == "downloading"
        assert items[0]["progress"] == 0.5

    def test_updated_at_is_refreshed_on_upsert(self, temp_db):
        """A re-upsert must overwrite a stale updated_at.

        Backdates the row instead of sleeping a real second. updated_at comes
        from SQLite's ``datetime('now')``, which has second granularity, so the
        old version burned 1s of every run just to make two values differ — and
        all it proved was "some time elapsed". Writing a known-old value pins the
        actual behaviour with no clock dependency.
        """
        queue_upsert("123", status="pending")
        stale = "2000-01-01 00:00:00"
        conn = get_conn()
        conn.execute(
            "UPDATE download_queue SET updated_at = ? WHERE workshop_id = ?", (stale, "123")
        )
        conn.commit()
        assert queue_load_all()[0]["updated_at"] == stale

        queue_upsert("123", status="done")

        assert queue_load_all()[0]["updated_at"] != stale


class TestQueueDelete:
    def test_delete_existing(self, temp_db):
        queue_upsert("111", status="pending")
        queue_upsert("222", status="pending")
        queue_delete("111")
        items = queue_load_all()
        assert len(items) == 1
        assert items[0]["workshop_id"] == "222"

    def test_delete_nonexistent(self, temp_db):
        queue_delete("nonexistent")  # should not raise
        assert queue_load_all() == []


class TestQueueLoadPending:
    def test_only_pending_and_downloading(self, temp_db):
        queue_upsert("1", status="pending")
        queue_upsert("2", status="downloading")
        queue_upsert("3", status="done")
        queue_upsert("4", status="failed")
        queue_upsert("5", status="cancelled")

        pending = queue_load_pending()
        assert len(pending) == 2
        pending_ids = {p["workshop_id"] for p in pending}
        assert pending_ids == {"1", "2"}

    def test_startup_recovery(self, temp_db):
        """Simulate restart: queue items survive."""
        queue_upsert("123", status="pending", name="Mod A")
        queue_upsert("456", status="downloading", name="Mod B", progress=0.3)

        # Simulate reload (new process)
        recovered = queue_load_pending()
        assert len(recovered) == 2
        names = {r["name"] for r in recovered}
        assert names == {"Mod A", "Mod B"}


class TestQueueClearDone:
    def test_clears_done_and_cancelled(self, temp_db):
        queue_upsert("1", status="done")
        queue_upsert("2", status="cancelled")
        queue_upsert("3", status="pending")
        queue_upsert("4", status="failed")

        queue_clear_done()
        remaining = queue_load_all()
        assert len(remaining) == 2
        ids = {r["workshop_id"] for r in remaining}
        assert ids == {"3", "4"}
