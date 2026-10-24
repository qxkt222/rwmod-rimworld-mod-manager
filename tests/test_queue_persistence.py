"""Test queue persistence: SQLite save/load, restart recovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.config import Config
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
from rwmod.queue import DownloadQueue


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


# ── Wave 6b: per-instance queue ownership ──────────────────────────


class TestQueueInstanceScope:
    def test_loads_and_mutations_are_instance_scoped(self, temp_db):
        queue_upsert("1", instance_id="alpha", name="A", status="done")
        queue_upsert("2", instance_id="alpha", name="B", status="pending")
        queue_upsert("3", instance_id="beta", name="C", status="done")

        assert {r["workshop_id"] for r in queue_load_all(instance_id="alpha")} == {"1", "2"}
        assert {r["workshop_id"] for r in queue_load_all(instance_id="beta")} == {"3"}
        assert {r["workshop_id"] for r in queue_load_pending(instance_id="alpha")} == {"2"}
        assert queue_load_pending(instance_id="beta") == []
        assert {r["instance_id"] for r in queue_load_all()} == {"alpha", "beta"}

        # Scoped cleanup only touches the named instance's rows.
        queue_clear_done(instance_id="alpha")
        assert {r["workshop_id"] for r in queue_load_all()} == {"2", "3"}
        queue_delete("2", instance_id="beta")  # wrong instance → no-op
        assert {r["workshop_id"] for r in queue_load_all()} == {"2", "3"}
        queue_delete("2", instance_id="alpha")
        assert {r["workshop_id"] for r in queue_load_all()} == {"3"}

    def test_unscoped_calls_keep_their_old_meaning(self, temp_db):
        queue_upsert("1", instance_id="alpha", status="done")
        queue_upsert("2", instance_id="beta", status="pending")
        assert {r["workshop_id"] for r in queue_load_all()} == {"1", "2"}
        assert [r["workshop_id"] for r in queue_load_pending()] == ["2"]
        queue_clear_done()
        assert [r["workshop_id"] for r in queue_load_all()] == ["2"]
        queue_delete("2")
        assert queue_load_all() == []


class TestQueueInstanceRecovery:
    def test_load_from_db_preserves_instance_id(self, temp_db):
        queue_upsert("111", instance_id="alpha", name="A", status="downloading")
        queue_upsert("222", instance_id="beta", name="B", status="pending")

        q = DownloadQueue()
        q._load_from_db()

        got = {i.id: (i.instance_id, i.status) for i in q.items}
        assert got["111"][0] == "alpha"
        assert got["111"][1] == "pending"  # interrupted download is requeued
        assert got["222"] == ("beta", "pending")

    def test_add_records_current_instance_and_dedupes_per_instance(self, temp_db, monkeypatch):
        import rwmod.queue as q_mod

        monkeypatch.setattr(q_mod, "current_instance_id", lambda: "alpha")
        q = DownloadQueue()
        first = q.add(["111"])
        assert first[0].instance_id == "alpha"
        assert q.add(["111"]) == []  # same instance + id is a duplicate

        other = q.add(["111"], instance_id="beta")  # explicit override
        assert other[0].instance_id == "beta"
        assert [i.instance_id for i in q.items] == ["alpha", "beta"]


class TestInstanceSwitchQueueGuard:
    """Queue-side half of the 409: the router sees the pending rows."""

    @staticmethod
    def _cfg(tmp_path: Path):
        from rwmod.config import InstanceConfig

        exe = tmp_path / "steamcmd.exe"
        default_mods = tmp_path / "DefaultMods"
        beta_mods = tmp_path / "BetaMods"
        default_mods.mkdir()
        beta_mods.mkdir()
        return Config(
            instances=[
                InstanceConfig(id="default", mods_dir=default_mods, steamcmd_path=exe),
                InstanceConfig(id="beta", mods_dir=beta_mods, steamcmd_path=exe),
            ],
            active_instance="default",
        )

    def test_pending_queue_item_blocks_activation(self, temp_db, tmp_path):
        from fastapi import HTTPException

        from rwmod.routers.config import activate_instance

        cfg = self._cfg(tmp_path)
        queue_upsert("111", instance_id="default", status="pending")

        with pytest.raises(HTTPException) as exc:
            activate_instance("beta", cfg=cfg)
        assert exc.value.status_code == 409
        assert cfg.active_instance == "default"

        queue_delete("111", instance_id="default")
        assert activate_instance("beta", cfg=cfg) == {"ok": True, "active_instance": "beta"}
        assert cfg.active_instance == "beta"


class TestQueueCompositePersistenceV4:
    """Wave 6c: two instances may persist the same workshop id side by side."""

    def test_two_instances_same_id_survive_restart(self, temp_db):
        queue_upsert("111", instance_id="alpha", name="A", status="pending")
        queue_upsert("111", instance_id="beta", name="B", status="downloading")

        q = DownloadQueue()
        q._load_from_db()

        assert {(i.instance_id, i.id, i.status) for i in q.items} == {
            ("alpha", "111", "pending"),
            ("beta", "111", "pending"),  # interrupted download requeued
        }

    def test_queue_add_persists_both_instances(self, temp_db, monkeypatch):
        import rwmod.queue as q_mod

        monkeypatch.setattr(q_mod, "current_instance_id", lambda: "alpha")
        q = DownloadQueue()
        q.add(["111"])
        q.add(["111"], instance_id="beta")

        reloaded = DownloadQueue()
        reloaded._load_from_db()
        assert {(i.instance_id, i.id) for i in reloaded.items} == {
            ("alpha", "111"),
            ("beta", "111"),
        }

    def test_clear_done_and_delete_are_scoped_for_the_same_workshop_id(self, temp_db):
        queue_upsert("111", instance_id="alpha", status="done")
        queue_upsert("111", instance_id="beta", status="done")

        queue_clear_done(instance_id="alpha")
        assert [(r["instance_id"], r["workshop_id"]) for r in queue_load_all()] == [("beta", "111")]

        queue_delete("111", instance_id="alpha")  # already gone → no-op
        assert [(r["instance_id"], r["workshop_id"]) for r in queue_load_all()] == [("beta", "111")]
        queue_delete("111", instance_id="beta")
        assert queue_load_all() == []

    def test_remove_same_id_only_deletes_its_own_instance_row(self, temp_db):
        q = DownloadQueue()
        q.add(["111"], instance_id="alpha")
        q.add(["111"], instance_id="beta")

        assert q.remove("111") is True

        assert [(r["instance_id"], r["workshop_id"]) for r in queue_load_all()] == [("beta", "111")]
        assert [(i.instance_id, i.id) for i in q.items] == [("beta", "111")]
