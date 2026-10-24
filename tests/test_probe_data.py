"""DATA-* probes: persistence & state layer.

Covers branches the existing suite leaves untested:
  database.py  — migration framework (fresh / intermediate / failing / re-init),
                 per-thread connection registry & close_db, WAL under threads,
                 queue column whitelist, workshop-id index lookups.
  backup.py    — cleanup keep-N boundaries, restore failure atomicity,
                 non-numeric workshop_id, foreign zip names.
  transfer.py  — full export/import round trip, secret redaction, tag/config
                 merge semantics, corrupt & hostile bundles.
  profile.py   — save/list/restore/delete profiles, atomic restore, traversal.
  undo.py      — snapshot pruning, ordering, undo_last.
  cache_db/mod_cache — three-layer cache hits, invalidation propagation.
  tags.py      — add_tag under a concurrent writer.

Isolation follows tests/conftest.py: every path (DB, profiles, undo, config)
is redirected under tmp_path — the real ~/.rwmod* files are never touched.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from rwmod.backup import (
    backup_mod,
    cleanup_backups,
    delete_backup,
    list_backups,
    restore_mod,
)
from rwmod.config import Config

# ── isolation fixtures ─────────────────────────────────────────────


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Redirect every persistence path into tmp_path (never the real home)."""
    import rwmod.database as db_mod
    import rwmod.profile as profile_mod
    import rwmod.transfer as transfer_mod
    import rwmod.undo as undo_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    db_mod.close_db()
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "probe.db")
    monkeypatch.setattr(db_mod, "_initialized", False)
    monkeypatch.setattr(Config, "CONFIG_PATH", tmp_path / "rwmod.toml")
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(transfer_mod, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(undo_mod, "UNDO_DIR", tmp_path / "undo")
    Config._cache = None
    Config._cache_ts = 0.0
    Config._cache_path_key = None

    ns = SimpleNamespace(
        tmp=tmp_path,
        home=home,
        db=db_mod,
        profile=profile_mod,
        undo=undo_mod,
        transfer=transfer_mod,
        profiles=tmp_path / "profiles",
        undo_dir=tmp_path / "undo",
        config_path=tmp_path / "rwmod.toml",
    )
    yield ns
    db_mod.close_db()
    Config._cache = None
    Config._cache_ts = 0.0
    Config._cache_path_key = None


def _make_mod(mods_dir: Path, folder: str, name: str) -> Path:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{name}</name><packageId>test.{folder}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    return mods_dir / folder


def _make_backup_zip(
    backup_dir: Path,
    workshop_id: str,
    folder: str,
    ts: str,
    content: str = "<ModMetaData/>",
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{workshop_id}__{folder}__{ts}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{folder}/About/About.xml", content)
    return path


# ── database.py: migration framework ───────────────────────────────


class TestMigrationFramework:
    def test_init_db_reopen_is_idempotent_and_preserves_data(self, env):
        env.db.init_db()
        version = env.db.get_conn().execute("PRAGMA user_version").fetchone()[0]
        env.db.record_download("1", "success", mod_name="kept")

        env.db.close_db()  # simulate a process restart
        env.db.init_db()
        env.db.init_db()  # and a redundant call

        assert env.db.get_conn().execute("PRAGMA user_version").fetchone()[0] == version
        assert env.db.get_download_stats()["total"] == 1
        assert env.db.get_download_history()[0]["mod_name"] == "kept"

    def test_migration_from_intermediate_version_applies_only_pending(self, env, monkeypatch):
        env.db.init_db()
        next_version = env.db.SCHEMA_VERSION + 1
        monkeypatch.setattr(
            env.db,
            "_MIGRATIONS",
            list(env.db._MIGRATIONS) + [(next_version, "CREATE TABLE probe_v2 (x INTEGER);")],
        )
        env.db.close_db()
        env.db.init_db()

        conn = env.db.get_conn()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == next_version
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "probe_v2" in tables

    def test_failed_migration_rolls_back_partial_schema(self, env, monkeypatch):
        """DATA-1: migrations run inside a transaction, so a half-applied
        migration must be rolled back and must not advance user_version."""
        env.db.init_db()
        next_version = env.db.SCHEMA_VERSION + 1
        monkeypatch.setattr(
            env.db,
            "_MIGRATIONS",
            list(env.db._MIGRATIONS)
            + [
                (
                    next_version,
                    "CREATE TABLE probe_partial (x INTEGER);\n"
                    "INSERT INTO probe_missing_table VALUES (1);",
                )
            ],
        )
        env.db.close_db()

        with pytest.raises(sqlite3.Error):
            env.db.init_db()

        conn = env.db.get_conn()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "probe_partial" not in tables, "partial migration schema survived the failure"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == env.db.SCHEMA_VERSION


# ── database.py: connections under threads ─────────────────────────


class TestConnectionPool:
    def test_concurrent_writes_from_many_threads(self, env):
        env.db.init_db()
        n_threads, per_thread = 6, 15
        errors: list[str] = []

        def worker(idx: int) -> None:
            try:
                for n in range(per_thread):
                    env.db.record_download(f"{idx}-{n}", "success")
            except Exception as exc:  # noqa: BLE001 — collected and asserted below
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert env.db.get_download_stats()["total"] == n_threads * per_thread
        assert env.db.get_conn().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_close_db_closes_other_threads_connections(self, env):
        env.db.init_db()
        captured: dict[str, sqlite3.Connection] = {}

        def worker() -> None:
            captured["conn"] = env.db.get_conn()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert id(captured["conn"]) in {id(c) for c in env.db._all_conns}

        env.db.close_db()
        with pytest.raises(sqlite3.ProgrammingError):
            captured["conn"].execute("SELECT 1")

    def test_get_conn_rebuilds_after_close_db(self, env):
        env.db.init_db()
        first = env.db.get_conn()
        env.db.close_db()
        second = env.db.get_conn()
        assert second is not first
        assert env.db.get_download_stats()["total"] == 0
        # the rebuilt connection must be usable (identity stability is DATA-9)
        assert second.execute("SELECT 1").fetchone()[0] == 1
        assert env.db.get_conn().execute("SELECT 1").fetchone()[0] == 1

    def test_get_conn_does_not_mistake_a_live_connection_for_a_closed_one(self, env):
        env.db.init_db()
        conn = env.db.get_conn()
        # Simulates id() recycling: after any earlier close_db() in the process,
        # a fresh connection can land on an id that is already in the set.
        env.db._closed_conn_ids.add(id(conn))

        assert env.db.get_conn() is conn, "live connection was discarded as 'closed'"
        assert len(env.db._all_conns) == 1

    def test_connection_registry_is_emptied_by_close_db(self, env):
        env.db.init_db()
        ids: list[int] = []

        def worker() -> None:
            ids.append(id(env.db.get_conn()))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        main_id = id(env.db.get_conn())
        registered = {id(c) for c in env.db._all_conns}
        assert set(ids) <= registered
        assert main_id in registered

        env.db.close_db()
        assert env.db._all_conns == set()

    def test_get_conn_waits_for_an_active_writer(self, env):
        holder = sqlite3.connect(str(env.db.DB_PATH), check_same_thread=False)
        holder.execute("CREATE TABLE t (x INTEGER)")
        holder.commit()
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO t VALUES (1)")

        released = threading.Event()

        def release() -> None:
            time.sleep(0.3)
            holder.rollback()
            released.set()

        releaser = threading.Thread(target=release)
        releaser.start()
        try:
            conn = env.db.get_conn()  # promised: wait up to 5s, don't fail
            assert conn is not None
        finally:
            releaser.join(timeout=5)
            if not released.is_set():
                holder.rollback()
            holder.close()


# ── database.py: crud / queue / index edges ────────────────────────


class TestDatabaseEdges:
    def test_queue_upsert_drops_unknown_columns(self, env):
        env.db.init_db()
        env.db.queue_upsert("123", status="pending", name="m")
        # A kwarg name is caller input: it must never reach the SQL as an identifier.
        env.db.queue_upsert("123", **{"status = 'x' WHERE 1=1 --": "y", "evil": 1})

        rows = env.db.queue_load_all()
        assert len(rows) == 1
        assert rows[0]["workshop_id"] == "123"
        assert "evil" not in rows[0]

        env.db.queue_upsert("123", status="done", progress=1.0, msg="ok")
        row = env.db.queue_load_all()[0]
        assert (row["status"], row["progress"], row["msg"]) == ("done", 1.0, "ok")
        assert row["name"] == "m"

    def test_queue_load_pending_filters_and_deletes(self, env):
        env.db.init_db()
        env.db.queue_upsert("1", status="pending")
        env.db.queue_upsert("2", status="downloading")
        env.db.queue_upsert("3", status="done")
        assert sorted(r["workshop_id"] for r in env.db.queue_load_pending()) == ["1", "2"]
        env.db.queue_clear_done()
        env.db.queue_delete("1")
        assert [r["workshop_id"] for r in env.db.queue_load_all()] == ["2"]

    def test_workshop_id_index_batch_lookup(self, env):
        env.db.init_db()
        env.db.upsert_local_mod_workshop_id("A", "111")
        env.db.upsert_local_mod_workshop_id("B", "111")
        env.db.upsert_local_mod_workshop_id("C", "222")

        assert sorted(env.db.find_local_mods_by_workshop_id("111")) == ["A", "B"]
        assert env.db.find_local_mods_by_workshop_ids([]) == {}
        batch = env.db.find_local_mods_by_workshop_ids(["111", "222", "333"])
        assert sorted(batch) == ["111", "222"]
        assert sorted(batch["111"]) == ["A", "B"]
        # Values are bound parameters, never interpolated SQL.
        assert env.db.find_local_mods_by_workshop_ids(["111' OR '1'='1"]) == {}

        env.db.remove_local_mod_workshop_id("A")
        assert env.db.find_local_mods_by_workshop_id("111") == ["B"]

    def test_set_last_updated_preserves_other_columns(self, env):
        env.db.init_db()
        mods = env.tmp / "Mods"
        _make_mod(mods, "MyMod", "My Mod")
        from rwmod.cache_db import get_or_refresh_metas

        get_or_refresh_metas(mods)  # writes name/package_id for MyMod

        env.db.set_last_updated("MyMod", 4242)
        row = (
            env.db.get_conn()
            .execute(
                "SELECT name, package_id, last_updated FROM local_mod_metadata WHERE folder='MyMod'"
            )
            .fetchone()
        )
        assert row["last_updated"] == 4242
        assert row["name"] == "My Mod"
        assert row["package_id"] == "test.MyMod"

        # A folder with no row at all still gets a timestamp.
        env.db.set_last_updated("Ghost", 7)
        assert env.db.get_last_updated("Ghost") == 7
        assert env.db.get_last_updated("NeverSeen") == 0

    def test_download_history_limit_and_filter(self, env):
        env.db.init_db()
        env.db.record_download("1", "success", mod_name="A")
        env.db.record_download("2", "failed", msg="boom")

        assert env.db.get_download_history(limit=0) == []
        rows = env.db.get_download_history(status="failed")
        assert len(rows) == 1 and rows[0]["msg"] == "boom"
        assert len(env.db.get_download_history(limit=1)) == 1
        env.db.clear_history()
        assert env.db.get_download_stats() == {"total": 0, "success": 0, "failed": 0}


# ── backup.py ──────────────────────────────────────────────────────


class TestCleanupBackups:
    def _three_with_ages(self, tmp_path: Path) -> tuple[Path, list[Path]]:
        backup_dir = tmp_path / "backups"
        paths = [
            _make_backup_zip(backup_dir, "111", "Mod", f"2024010{n}_000000") for n in (1, 2, 3)
        ]
        now = time.time()
        for n, p in enumerate(paths):
            os.utime(p, (now - 300 + n, now - 300 + n))  # oldest → newest
        return backup_dir, paths

    def test_keep_zero_deletes_all(self, tmp_path: Path):
        backup_dir, paths = self._three_with_ages(tmp_path)
        assert cleanup_backups(backup_dir, keep_per_mod=0) == 3
        assert list(backup_dir.glob("*.zip")) == []
        assert all(not p.exists() for p in paths)

    def test_keep_one_leaves_only_the_newest(self, tmp_path: Path):
        backup_dir, paths = self._three_with_ages(tmp_path)
        assert cleanup_backups(backup_dir, keep_per_mod=1) == 2
        assert [p.name for p in backup_dir.glob("*.zip")] == [paths[-1].name]

    def test_keep_above_count_deletes_nothing(self, tmp_path: Path):
        backup_dir, _ = self._three_with_ages(tmp_path)
        assert cleanup_backups(backup_dir, keep_per_mod=10) == 0
        assert len(list(backup_dir.glob("*.zip"))) == 3

    def test_grouping_is_per_workshop_id(self, tmp_path: Path):
        backup_dir = tmp_path / "backups"
        a1 = _make_backup_zip(backup_dir, "111", "Mod", "20240101_000000")
        a2 = _make_backup_zip(backup_dir, "111", "Mod", "20240102_000000")
        b1 = _make_backup_zip(backup_dir, "222", "Other", "20240101_000000")
        now = time.time()
        os.utime(a1, (now - 100, now - 100))
        os.utime(a2, (now, now))
        os.utime(b1, (now - 50, now - 50))

        # keep 1 per mod: one file of 111 goes, 222 is untouched.
        assert cleanup_backups(backup_dir, keep_per_mod=1) == 1
        assert not a1.exists()
        assert {p.name for p in backup_dir.glob("*.zip")} == {a2.name, b1.name}

    def test_non_zip_files_and_missing_dir(self, tmp_path: Path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "notes.txt").write_text("keep me")
        assert cleanup_backups(backup_dir, keep_per_mod=0) == 0
        assert (backup_dir / "notes.txt").exists()
        assert cleanup_backups(tmp_path / "missing", keep_per_mod=1) == 0


class TestBackupMod:
    def test_rejects_non_numeric_workshop_id(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        _make_mod(mods_dir, "MyMod", "My Mod")
        backup_dir = tmp_path / "backups"

        assert backup_mod(mods_dir, "../../evil", "MyMod", backup_dir) is None
        assert backup_mod(mods_dir, "12a", "MyMod", backup_dir) is None
        assert not backup_dir.exists() or list(backup_dir.glob("*")) == []

    def test_backup_and_restore_roundtrip_with_unicode_name(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mod = _make_mod(mods_dir, "我的模组 v2", "Mine")
        (mod / "extra.bin").write_bytes(b"\x00\x01")
        backup_dir = tmp_path / "backups"

        zip_path = backup_mod(mods_dir, "999", "我的模组 v2", backup_dir)
        assert zip_path is not None and zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            assert "我的模组 v2/extra.bin" in zf.namelist()

        shutil.rmtree(mod)
        result = restore_mod(mods_dir, "999", backup_dir)
        assert result["ok"] is True
        assert (mods_dir / "我的模组 v2" / "extra.bin").read_bytes() == b"\x00\x01"


class TestRestoreModFailures:
    def test_corrupt_backup_leaves_target_and_no_temp_dirs(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mod = _make_mod(mods_dir, "MyMod", "Live")
        (mod / "keep.txt").write_text("original")
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        (backup_dir / "123__MyMod__20240101_000000.zip").write_bytes(b"definitely not a zip")

        result = restore_mod(mods_dir, "123", backup_dir)
        assert result["ok"] is False
        assert (mod / "keep.txt").read_text() == "original"
        assert list(mods_dir.glob(".rwmod_restore_*")) == []
        assert list(mods_dir.glob(".rwmod_old_*")) == []

    def test_archive_without_single_top_level_dir_is_rejected(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        two_roots = backup_dir / "123__MyMod__20240101_000000.zip"
        with zipfile.ZipFile(two_roots, "w") as zf:
            zf.writestr("MyMod/About/About.xml", "<ModMetaData/>")
            zf.writestr("stray/other.txt", "x")
        result = restore_mod(mods_dir, "123", backup_dir)
        assert result["ok"] is False
        assert list(mods_dir.iterdir()) == []

        flat = backup_dir / "123__MyMod__20240102_000000.zip"
        with zipfile.ZipFile(flat, "w") as zf:
            zf.writestr("loose_file.txt", "x")
        result = restore_mod(mods_dir, "123", backup_dir, backup_filename=flat.name)
        assert result["ok"] is False
        assert list(mods_dir.iterdir()) == []

    def test_restore_replaces_existing_folder_and_cleans_swaps(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        backup_dir = tmp_path / "backups"
        _make_backup_zip(
            backup_dir,
            "123",
            "MyMod",
            "20240101_000000",
            content="<ModMetaData><name>RESTORED</name></ModMetaData>",
        )
        live = _make_mod(mods_dir, "MyMod", "Stale")
        (live / "leftover.txt").write_text("should disappear")

        result = restore_mod(mods_dir, "123", backup_dir)
        assert result["ok"] is True
        assert result["restored_folder"] == "MyMod"
        restored = mods_dir / "MyMod" / "About" / "About.xml"
        assert "RESTORED" in restored.read_text()
        assert not (mods_dir / "MyMod" / "leftover.txt").exists()
        assert list(mods_dir.glob(".rwmod_old_*")) == []
        assert list(mods_dir.glob(".rwmod_restore_*")) == []

    def test_restore_invalid_workshop_id(self, tmp_path: Path):
        assert restore_mod(tmp_path / "Mods", "../etc", tmp_path / "b")["ok"] is False
        assert restore_mod(tmp_path / "Mods", "", tmp_path / "b")["ok"] is False
        assert restore_mod(tmp_path / "Mods", "123", tmp_path / "b")["ok"] is False

    def test_failed_swap_never_destroys_the_original(self, tmp_path: Path, monkeypatch):
        mods_dir = tmp_path / "Mods"
        live = _make_mod(mods_dir, "MyMod", "Live")
        (live / "precious.txt").write_text("user data")
        backup_dir = tmp_path / "backups"
        _make_backup_zip(backup_dir, "123", "MyMod", "20240101_000000", content="<ModMetaData/>")

        real_rename = Path.rename

        def flaky_rename(self: Path, target: Path) -> Path:
            # (2) promoting the restored copy fails, then (3) the rollback fails too
            if self.parent.name.startswith(".rwmod_restore_") or self.name.startswith(
                ".rwmod_old_"
            ):
                raise OSError("simulated filesystem lock")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky_rename)
        result = restore_mod(mods_dir, "123", backup_dir)

        assert result["ok"] is False
        survivors = list(mods_dir.rglob("precious.txt"))
        assert survivors, "the original mod content was destroyed by a failed restore"


class TestListBackupsAndPaths:
    def test_list_skips_foreign_names_and_filters(self, tmp_path: Path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        good = _make_backup_zip(backup_dir, "123", "Mod", "20240101_000000")
        (backup_dir / "not-a-backup.zip").write_bytes(b"x")
        (backup_dir / "notes.txt").write_text("x")

        listed = list_backups(backup_dir)
        assert [b["filename"] for b in listed] == [good.name]
        assert listed[0]["workshop_id"] == "123"
        assert listed[0]["folder_name"] == "Mod"
        assert listed[0]["timestamp"].startswith("2024-01-01")
        assert list_backups(backup_dir, workshop_id="999") == []
        assert list_backups(tmp_path / "missing") == []

    def test_delete_backup_rejects_unsafe_names(self, tmp_path: Path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        victim = tmp_path / "victim.zip"
        victim.write_bytes(b"data")
        keep = _make_backup_zip(backup_dir, "123", "Mod", "20240101_000000")

        for bad in (
            "",
            ".",
            "..",
            "../victim.zip",
            "..\\victim.zip",
            "sub/victim.zip",
            str(victim),
            "C:\\Windows\\victim.zip",
        ):
            assert delete_backup(backup_dir, bad) is False
        assert victim.exists()
        assert delete_backup(backup_dir, "does-not-exist.zip") is False
        assert delete_backup(backup_dir, keep.name) is True
        assert not keep.exists()


# ── transfer.py ────────────────────────────────────────────────────


@pytest.fixture
def xfer(env: SimpleNamespace, tmp_path: Path) -> Config:
    cfg = Config(
        steamcmd_path=tmp_path / "steamcmd.exe",
        mods_dir=tmp_path / "Mods",
        backup_dir=tmp_path / "backups",
    )
    cfg.mods_dir.mkdir(exist_ok=True)
    return cfg


def _write_bundle(
    path: Path,
    *,
    profiles: dict[str, str] | None = None,
    backups: list[str] | None = None,
    tags: object = ...,
    config_toml: str | None = None,
    manifest: dict | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in (profiles or {}).items():
            zf.writestr(f"profiles/{name}", content)
        for name in backups or []:
            zf.writestr(f"backups/{name}", b"PK\x03\x04")
        if tags is not ...:
            zf.writestr("tags.json", json.dumps(tags))
        if config_toml is not None:
            zf.writestr("config.toml", config_toml)
        if manifest is not None:
            zf.writestr("manifest.json", json.dumps(manifest))
    return path


class TestExportBundle:
    def test_export_packages_profiles_backups_tags_and_config(self, env, xfer):
        env.db.init_db()
        from rwmod.tags import add_tag

        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "P1.xml").write_text(
            "<ModsConfig><activeMods><li>a</li></activeMods></ModsConfig>", encoding="utf-8"
        )
        (env.profiles / "P2.xml").write_text("<ModsConfig/>", encoding="utf-8")
        _make_backup_zip(xfer.backup_dir, "111", "Mod", "20240101_000000")
        add_tag("Mod", "QoL")
        xfer.steam_api_key = "sk_live_secret"

        out = env.tmp / "export.rwmod"
        result = env.transfer.export_bundle(xfer, out, include_backups=True)

        assert result["ok"] is True
        assert (result["profiles"], result["backups"], result["tags"]) == (2, 1, True)
        assert result["path"] == str(out) and result["size_mb"] >= 0

        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
            config_toml = zf.read("config.toml").decode()
            tags = json.loads(zf.read("tags.json"))
            profile_1 = zf.read("profiles/default/P1.xml").decode()

        assert {
            "manifest.json",
            "config.toml",
            "tags.json",
            "profiles/default/P1.xml",
            "profiles/default/P2.xml",
            "backups/111__Mod__20240101_000000.zip",
        } <= names
        assert manifest["app"] == "rwmod" and manifest["version"] == 2
        assert [i["id"] for i in manifest["instances"]] == ["default"]
        assert (manifest["profiles"], manifest["backups"], manifest["tags"]) == (2, 1, True)
        assert manifest["has_steam_api_key"] is True
        assert "sk_live_secret" not in config_toml
        assert tags == {"version": 2, "instances": {"default": {"Mod": ["QoL"]}}}
        assert "<li>a</li>" in profile_1

    def test_export_honours_include_backups_false(self, env, xfer):
        env.db.init_db()
        _make_backup_zip(xfer.backup_dir, "111", "Mod", "20240101_000000")
        out = env.tmp / "nb.rwmod"

        result = env.transfer.export_bundle(xfer, out, include_backups=False)
        assert result["backups"] == 0
        with zipfile.ZipFile(out) as zf:
            assert not any(n.startswith("backups/") for n in zf.namelist())
            assert json.loads(zf.read("manifest.json"))["backups"] == 0

    def test_export_without_profiles_dir_or_tags_table(self, env, xfer):
        """PROFILES_DIR absent + no mod_tags table → 0 profiles, tags False, no crash."""
        assert not env.profiles.exists()
        out = env.tmp / "empty.rwmod"
        result = env.transfer.export_bundle(xfer, out, include_backups=True)

        assert result["ok"] is True
        assert (result["profiles"], result["backups"], result["tags"]) == (0, 0, False)
        with zipfile.ZipFile(out) as zf:
            assert "tags.json" not in zf.namelist()
            assert json.loads(zf.read("manifest.json"))["tags"] is False

    def test_export_config_secret_flag(self, env, xfer):
        xfer.steam_api_key = "sk_live_secret"
        assert "sk_live_secret" in env.transfer._export_config(xfer, include_secrets=True)
        assert env.transfer._export_config(xfer) == "\n"

        xfer.steam_api_key = ""
        out = env.tmp / "nokey.rwmod"
        env.transfer.export_bundle(xfer, out, include_backups=False)
        with zipfile.ZipFile(out) as zf:
            assert json.loads(zf.read("manifest.json"))["has_steam_api_key"] is False


class TestImportBundle:
    def test_import_restores_everything_and_persists_key(self, env, xfer):
        env.db.init_db()
        from rwmod.tags import get_tags

        bundle = _write_bundle(
            env.tmp / "in.rwmod",
            profiles={"P1.xml": "<ModsConfig/>"},
            backups=["111__Mod__20240101_000000.zip"],
            tags={"Mod": ["QoL"]},
            config_toml='steam_api_key = "sk_imported"\n',
            manifest={"app": "rwmod", "version": 1},
        )
        result = env.transfer.import_bundle(xfer, bundle)

        assert result["ok"] is True
        assert (result["profiles"], result["backups"]) == (1, 1)
        assert (env.profiles / "default" / "P1.xml").exists()
        assert (xfer.backup_dir / "111__Mod__20240101_000000.zip").exists()
        assert get_tags("Mod") == ["QoL"]
        assert xfer.steam_api_key == "sk_imported"
        assert "sk_imported" in env.config_path.read_text(encoding="utf-8")
        # the scratch extraction dir must not survive
        assert [p for p in env.tmp.iterdir() if p.name.startswith(".rwmod_import")] == []

    def test_import_keeps_live_key_when_bundle_has_none(self, env, xfer):
        env.db.init_db()
        xfer.steam_api_key = "keep_me"
        bundle = _write_bundle(
            env.tmp / "in.rwmod", profiles={"P.xml": "<ModsConfig/>"}, config_toml="\n"
        )
        result = env.transfer.import_bundle(xfer, bundle)

        assert result["ok"] is True
        assert xfer.steam_api_key == "keep_me"
        assert not env.config_path.exists()  # nothing to persist → save() not called

    def test_import_tags_twice_is_idempotent(self, env, xfer):
        env.db.init_db()
        from rwmod.tags import get_tags, list_all_tags

        bundle = _write_bundle(env.tmp / "in.rwmod", tags={"Mod": ["QoL", "Content"]})
        assert env.transfer.import_bundle(xfer, bundle)["ok"] is True
        assert env.transfer.import_bundle(xfer, bundle)["ok"] is True

        assert get_tags("Mod") == ["Content", "QoL"]
        assert {t["tag"]: t["count"] for t in list_all_tags()} == {"Content": 1, "QoL": 1}

    def test_import_invalid_tags_json_is_ignored(self, env, xfer):
        env.db.init_db()
        env.profiles.mkdir(parents=True, exist_ok=True)
        bundle = _write_bundle(env.tmp / "in.rwmod", profiles={"P.xml": "<ModsConfig/>"})
        with zipfile.ZipFile(bundle, "a") as zf:
            zf.writestr("tags.json", "{not json at all")

        result = env.transfer.import_bundle(xfer, bundle)
        assert result["ok"] is True
        assert result["profiles"] == 1
        assert (env.profiles / "default" / "P.xml").exists()

    def test_import_tags_of_wrong_shape_are_dropped_without_failing(self, env, xfer):
        env.db.init_db()
        env.profiles.mkdir(parents=True, exist_ok=True)
        from rwmod.tags import list_all_tags

        bundle = _write_bundle(
            env.tmp / "in.rwmod", profiles={"P.xml": "<ModsConfig/>"}, tags=["not", "a", "mapping"]
        )
        result = env.transfer.import_bundle(xfer, bundle)

        assert result["ok"] is True  # tags are best-effort; the rest still lands
        assert (env.profiles / "default" / "P.xml").exists()
        assert list_all_tags() == []

    def test_import_broken_config_toml_keeps_live_values(self, env, xfer):
        env.db.init_db()
        xfer.steam_api_key = "keep_me"
        bundle = _write_bundle(env.tmp / "in.rwmod", config_toml="steam_api_key = [unterminated\n")
        result = env.transfer.import_bundle(xfer, bundle)

        assert result["ok"] is True
        assert xfer.steam_api_key == "keep_me"

    def test_import_without_manifest_still_succeeds(self, env, xfer):
        """Import never reads manifest.json (see the DATA-8 note in the report)."""
        env.db.init_db()
        bundle = _write_bundle(env.tmp / "in.rwmod", profiles={"P.xml": "<ModsConfig/>"})
        assert env.transfer.import_bundle(xfer, bundle)["ok"] is True

    def test_import_missing_or_corrupt_bundle(self, env, xfer):
        env.db.init_db()
        assert env.transfer.import_bundle(xfer, env.tmp / "nope.rwmod")["ok"] is False

        corrupt = env.tmp / "corrupt.rwmod"
        corrupt.write_bytes(b"PK\x03\x04 truncated")
        result = env.transfer.import_bundle(xfer, corrupt)
        assert result["ok"] is False
        assert [p for p in env.tmp.iterdir() if p.name.startswith(".rwmod_import")] == []

    def test_import_directory_entry_named_like_a_profile(self, env, xfer):
        env.db.init_db()
        bundle = env.tmp / "hostile.rwmod"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("profiles/dir.xml/", "")  # directory entry, not a file
            zf.writestr("profiles/ok.xml", "<ModsConfig/>")

        try:
            result = env.transfer.import_bundle(xfer, bundle)
        except Exception as exc:  # noqa: BLE001 — the point of the probe
            pytest.fail(f"import_bundle raised {type(exc).__name__} instead of returning: {exc}")
        assert result["ok"] is False


# ── profile.py ─────────────────────────────────────────────────────


class TestSaveProfile:
    def test_save_from_xml_path(self, env):
        src = env.tmp / "ModsConfig.xml"
        src.write_text(
            "<ModsConfig><activeMods><li>a</li><li>b</li></activeMods></ModsConfig>",
            encoding="utf-8",
        )
        result = env.profile.save_profile("My Profile", src)

        assert result["ok"] is True and result["mod_count"] == 2
        assert (
            (env.profiles / "default" / "My Profile.xml")
            .read_text(encoding="utf-8")
            .startswith("<ModsConfig>")
        )

    def test_save_from_xml_string(self, env):
        result = env.profile.save_profile(
            "Inline", "<ModsConfig><activeMods><li>x</li></activeMods></ModsConfig>"
        )
        assert result["ok"] is True and result["mod_count"] == 1

    def test_save_rejects_invalid_xml_and_missing_file(self, env):
        bad = env.profile.save_profile("Bad", "<ModsConfig>")
        assert bad["ok"] is False and "XML" in bad["msg"]
        assert not (env.profiles / "default" / "Bad.xml").exists()

        missing = env.profile.save_profile("Missing", env.tmp / "nope.xml")
        assert missing["ok"] is False

    def test_save_unreadable_source_returns_error(self, env):
        src = env.tmp / "not-a-file.xml"
        src.mkdir()  # exists() is True, read_text() is not
        result = env.profile.save_profile("Dir", src)
        assert result["ok"] is False and "读取失败" in result["msg"]
        assert not (env.profiles / "default" / "Dir.xml").exists()

    def test_save_name_is_sanitized_against_traversal(self, env):
        result = env.profile.save_profile("../../evil", "<ModsConfig/>")
        assert result["ok"] is True
        assert not (env.tmp / "evil.xml").exists()
        assert (env.profiles / "default" / "evil.xml").exists()

        unnamed = env.profile.save_profile("", "<ModsConfig/>")
        assert unnamed["ok"] is True
        assert (env.profiles / "default" / "unnamed.xml").exists()


class TestListProfiles:
    def test_lists_newest_first_with_metadata(self, env):
        env.profiles.mkdir(parents=True, exist_ok=True)
        older = env.profiles / "old.xml"
        older.write_text(
            "<ModsConfig><activeMods><li>1</li><li>2</li></activeMods></ModsConfig>",
            encoding="utf-8",
        )
        newer = env.profiles / "new.xml"
        newer.write_text("<ModsConfig/>", encoding="utf-8")
        now = time.time()
        os.utime(older, (now - 100, now - 100))
        os.utime(newer, (now, now))

        rows = env.profile.list_profiles()
        assert [r["name"] for r in rows] == ["new", "old"]
        assert rows[1]["mod_count"] == 2
        assert rows[0]["mod_count"] == 0
        assert rows[0]["size_kb"] >= 0
        assert rows[0]["saved_at"].startswith("2")

    def test_creates_dir_and_tolerates_malformed_xml(self, env):
        assert not env.profiles.exists()
        assert env.profile.list_profiles() == []
        assert env.profiles.exists()

        (env.profiles / "broken.xml").write_text("not xml at all", encoding="utf-8")
        assert env.profile.list_profiles()[0]["mod_count"] == 0

    def test_skips_directories_named_like_profiles(self, env):
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "trap.xml").mkdir()
        (env.profiles / "real.xml").write_text("<ModsConfig/>", encoding="utf-8")

        assert [r["name"] for r in env.profile.list_profiles()] == ["real"]


class TestRestoreProfile:
    def test_restore_writes_target_and_backs_up_previous(self, env):
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "P.xml").write_text("<ModsConfig>NEW</ModsConfig>", encoding="utf-8")
        target = env.tmp / "RimWorld" / "Config" / "ModsConfig.xml"
        target.parent.mkdir(parents=True)
        target.write_text("<ModsConfig>OLD</ModsConfig>", encoding="utf-8")

        result = env.profile.restore_profile("P", target)
        assert result["ok"] is True
        assert target.read_text(encoding="utf-8") == "<ModsConfig>NEW</ModsConfig>"
        assert (target.parent / "ModsConfig.xml.rwmod.bak").read_text(
            encoding="utf-8"
        ) == "<ModsConfig>OLD</ModsConfig>"
        assert list(target.parent.glob("*.rwmod.tmp")) == []

    def test_restore_creates_missing_target_directory(self, env):
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "P.xml").write_text("<ModsConfig>NEW</ModsConfig>", encoding="utf-8")
        target = env.tmp / "fresh" / "nested" / "ModsConfig.xml"

        assert env.profile.restore_profile("P", target)["ok"] is True
        assert target.read_text(encoding="utf-8") == "<ModsConfig>NEW</ModsConfig>"

    def test_restore_missing_profile(self, env):
        assert env.profile.restore_profile("nope", env.tmp / "x.xml")["ok"] is False

    def test_restore_recovers_from_a_stale_temp_file(self, env):
        """A crashed earlier restore leaves *.rwmod.tmp behind; the next one wins."""
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "P.xml").write_text("<ModsConfig>NEW</ModsConfig>", encoding="utf-8")
        target = env.tmp / "ModsConfig.xml"
        stale = env.tmp / "ModsConfig.xml.rwmod.tmp"
        stale.write_text("STALE PARTIAL", encoding="utf-8")

        assert env.profile.restore_profile("P", target)["ok"] is True
        assert target.read_text(encoding="utf-8") == "<ModsConfig>NEW</ModsConfig>"
        assert not stale.exists()

    def test_restore_reports_failure_when_backup_path_is_blocked(self, env):
        """A failed swap must leave the live ModsConfig.xml untouched."""
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "P.xml").write_text("<ModsConfig>NEW</ModsConfig>", encoding="utf-8")
        target = env.tmp / "ModsConfig.xml"
        target.write_text("<ModsConfig>LIVE</ModsConfig>", encoding="utf-8")
        blocked = env.tmp / "ModsConfig.xml.rwmod.bak"
        blocked.mkdir()
        (blocked / "occupied.txt").write_text("x")  # non-empty dir cannot be replaced

        result = env.profile.restore_profile("P", target)

        if platform.system() == "Windows":
            assert result["ok"] is False
            assert target.read_text(encoding="utf-8") == "<ModsConfig>LIVE</ModsConfig>"
            assert list(env.tmp.glob("*.rwmod.tmp")) == []

    def test_restore_unreadable_profile_returns_error_not_crash(self, env):
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "bad.xml").write_bytes(b"<ModsConfig>\xff\xfe</ModsConfig>")
        target = env.tmp / "ModsConfig.xml"
        target.write_text("ORIGINAL", encoding="utf-8")

        try:
            result = env.profile.restore_profile("bad", target)
        except Exception as exc:  # noqa: BLE001 — the point of the probe
            pytest.fail(f"restore_profile raised {type(exc).__name__}: {exc}")
        assert result["ok"] is False
        assert target.read_text(encoding="utf-8") == "ORIGINAL"


class TestDeleteAndResolve:
    def test_delete_profile(self, env):
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "P.xml").write_text("<ModsConfig/>", encoding="utf-8")

        assert env.profile.delete_profile("P") is True
        assert not (env.profiles / "default" / "P.xml").exists()
        assert env.profile.delete_profile("P") is False

    def _ludeon_dir(self) -> tuple[str, ...]:
        if platform.system() == "Windows":
            return ("AppData", "LocalLow", "Ludeon Studios")
        if platform.system() == "Darwin":
            return ("Library", "Application Support", "Ludeon Studios")
        return (".config", "unity3d", "Ludeon Studios")

    def test_resolve_finds_ludeon_studios_config(self, env):
        path = env.home.joinpath(
            *self._ludeon_dir(), "RimWorld by Ludeon Studios", "Config", "ModsConfig.xml"
        )
        path.parent.mkdir(parents=True)
        path.write_text("<ModsConfig/>", encoding="utf-8")

        assert env.profile.resolve_modsconfig_path(env.tmp / "RimWorld") == path

    def test_resolve_falls_back_to_rimworld_dir(self, env):
        # A RimWorld* dir without a Config/ModsConfig.xml must not stop the search.
        (env.home.joinpath(*self._ludeon_dir(), "RimWorld empty")).mkdir(parents=True)
        rimworld = env.tmp / "RimWorld"
        rimworld.mkdir()
        local = rimworld / "ModsConfig.xml"
        local.write_text("<ModsConfig/>", encoding="utf-8")

        assert env.profile.resolve_modsconfig_path(rimworld) == local

    def test_resolve_returns_none_when_nothing_exists(self, env):
        assert env.profile.resolve_modsconfig_path(env.tmp / "RimWorld") is None


# ── undo.py ────────────────────────────────────────────────────────


class TestUndoSnapshots:
    def test_missing_source_returns_none(self, env):
        assert env.undo.snapshot_modsconfig(env.tmp / "nope.xml") is None

    def test_label_is_sanitized(self, env):
        src = env.tmp / "ModsConfig.xml"
        src.write_text("<ModsConfig/>", encoding="utf-8")
        snap = env.undo.snapshot_modsconfig(src, label="a b/c:*?  ")
        assert snap is not None and snap.name.endswith("_abc.xml")

    def test_prune_keeps_the_twenty_newest(self, env):
        src = env.tmp / "ModsConfig.xml"
        base = time.time() - 1000
        for i in range(25):
            src.write_text(f"<ModsConfig>{i}</ModsConfig>", encoding="utf-8")
            os.utime(src, (base + i, base + i))
            assert env.undo.snapshot_modsconfig(src, label=f"s{i:02d}") is not None

        snaps = env.undo.list_undo_snapshots()
        assert len(snaps) == 20
        assert snaps[0].name.endswith("_s24.xml")
        assert snaps[-1].name.endswith("_s05.xml")

    def test_undo_last_restores_and_consumes_latest(self, env):
        src = env.tmp / "ModsConfig.xml"
        src.write_text("<ModsConfig>FIRST</ModsConfig>", encoding="utf-8")
        first = env.undo.snapshot_modsconfig(src, label="one")
        assert first is not None
        src.write_text("<ModsConfig>SECOND</ModsConfig>", encoding="utf-8")
        # the live file is rewritten by the destructive op → newer mtime; make it
        # explicit because Windows timestamp granularity can tie at ~15ms
        later = time.time() + 10
        os.utime(src, (later, later))
        second = env.undo.snapshot_modsconfig(src, label="two")
        assert second is not None and first != second

        target = env.tmp / "live" / "ModsConfig.xml"
        result = env.undo.undo_last(target)
        assert result["ok"] is True and result["restored"] == second.name
        assert target.read_text(encoding="utf-8") == "<ModsConfig>SECOND</ModsConfig>"
        assert not second.exists()
        assert env.undo.list_undo_snapshots() == [first]

    def test_undo_last_without_snapshots(self, env):
        env.undo_dir.mkdir(parents=True, exist_ok=True)
        result = env.undo.undo_last(env.tmp / "ModsConfig.xml")
        assert result["ok"] is False and result["restored"] is None

    def test_undo_last_reports_failure_and_keeps_the_snapshot(self, env):
        src = env.tmp / "ModsConfig.xml"
        src.write_text("<ModsConfig>A</ModsConfig>", encoding="utf-8")
        snap = env.undo.snapshot_modsconfig(src)
        assert snap is not None

        blocker = env.tmp / "not_a_dir"
        blocker.write_text("x")  # parent.mkdir() cannot succeed under a file
        result = env.undo.undo_last(blocker / "ModsConfig.xml")

        assert result["ok"] is False and result["restored"] is None
        assert snap.exists(), "a failed undo must not consume the snapshot"

    def test_snapshot_records_its_own_timestamp(self, env):
        src = env.tmp / "ModsConfig.xml"
        src.write_text("<ModsConfig/>", encoding="utf-8")
        old = time.time() - 86400
        os.utime(src, (old, old))

        before = time.time()
        snap = env.undo.snapshot_modsconfig(src)
        assert snap is not None
        assert snap.stat().st_mtime >= before - 5, (
            f"snapshot mtime {snap.stat().st_mtime} should record the snapshot time, "
            f"not the source mtime {old}"
        )

    def test_two_operations_in_one_second_are_two_snapshots(self, env):
        src = env.tmp / "ModsConfig.xml"
        src.write_text("<ModsConfig>A</ModsConfig>", encoding="utf-8")
        first = env.undo.snapshot_modsconfig(src, label="sort")
        src.write_text("<ModsConfig>B</ModsConfig>", encoding="utf-8")
        second = env.undo.snapshot_modsconfig(src, label="sort")

        assert first is not None and second is not None
        assert first != second, "second same-second snapshot overwrote the first"
        assert len(env.undo.list_undo_snapshots()) == 2


# ── multi-instance scoping (Wave 6a) ───────────────────────────────


class TestInstanceScopedPersistence:
    def test_profiles_are_scoped_per_instance(self, env):
        env.profile.save_profile("P", "<ModsConfig/>", instance_id="alpha")
        env.profile.save_profile(
            "P",
            "<ModsConfig><activeMods><li>x</li></activeMods></ModsConfig>",
            instance_id="beta",
        )

        assert [p["name"] for p in env.profile.list_profiles("alpha")] == ["P"]
        assert env.profile.list_profiles("beta")[0]["mod_count"] == 1
        assert (env.profiles / "alpha" / "P.xml").exists()
        assert (env.profiles / "beta" / "P.xml").exists()

        assert env.profile.delete_profile("P", instance_id="alpha") is True
        assert env.profile.list_profiles("alpha") == []
        assert [p["name"] for p in env.profile.list_profiles("beta")] == ["P"]

    def test_legacy_root_profiles_migrate_to_default(self, env):
        env.profiles.mkdir(parents=True, exist_ok=True)
        (env.profiles / "Old.xml").write_text("<ModsConfig/>", encoding="utf-8")

        assert [p["name"] for p in env.profile.list_profiles()] == ["Old"]
        assert (env.profiles / "default" / "Old.xml").exists()
        assert not (env.profiles / "Old.xml").exists()

    def test_undo_snapshots_are_scoped_per_instance(self, env):
        src_a = env.tmp / "a.xml"
        src_a.write_text("<ModsConfig>ALPHA</ModsConfig>", encoding="utf-8")
        src_b = env.tmp / "b.xml"
        src_b.write_text("<ModsConfig>BETA</ModsConfig>", encoding="utf-8")

        snap_a = env.undo.snapshot_modsconfig(src_a, instance_id="alpha")
        snap_b = env.undo.snapshot_modsconfig(src_b, instance_id="beta")
        assert snap_a is not None and snap_b is not None
        assert (env.undo_dir / "alpha" / snap_a.name).exists()
        assert (env.undo_dir / "beta" / snap_b.name).exists()

        assert len(env.undo.list_undo_snapshots("alpha")) == 1
        assert len(env.undo.list_undo_snapshots("beta")) == 1

        target = env.tmp / "live" / "ModsConfig.xml"
        result = env.undo.undo_last(target, instance_id="alpha")
        assert result["ok"] is True
        assert target.read_text(encoding="utf-8") == "<ModsConfig>ALPHA</ModsConfig>"
        assert env.undo.list_undo_snapshots("alpha") == []
        assert len(env.undo.list_undo_snapshots("beta")) == 1

    def test_legacy_root_undo_snapshots_migrate_to_default(self, env):
        env.undo_dir.mkdir(parents=True, exist_ok=True)
        (env.undo_dir / "20240101_000000_sort.xml").write_text("<ModsConfig/>", encoding="utf-8")

        snaps = env.undo.list_undo_snapshots()
        assert [p.name for p in snaps] == ["20240101_000000_sort.xml"]
        assert (env.undo_dir / "default" / "20240101_000000_sort.xml").exists()

    def test_export_scopes_profiles_and_tags_per_instance(self, env, xfer):
        env.db.init_db()
        from rwmod.tags import add_tag

        second = xfer.add_instance(name="Second", mods_dir=env.tmp / "Mods2")
        env.profile.save_profile("A", "<ModsConfig/>", instance_id="default")
        env.profile.save_profile("B", "<ModsConfig/>", instance_id=second.id)
        add_tag("Mod", "DefaultTag", instance_id="default")
        add_tag("Mod", "SecondTag", instance_id=second.id)

        out = env.tmp / "multi.rwmod"
        result = env.transfer.export_bundle(xfer, out, include_backups=False)
        assert result["profiles"] == 2

        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
            tags = json.loads(zf.read("tags.json"))

        assert "profiles/default/A.xml" in names
        assert f"profiles/{second.id}/B.xml" in names
        assert [i["id"] for i in manifest["instances"]] == ["default", second.id]
        assert tags["instances"]["default"] == {"Mod": ["DefaultTag"]}
        assert tags["instances"][second.id] == {"Mod": ["SecondTag"]}

    def test_import_v2_bundle_restores_profiles_per_instance(self, env, xfer):
        env.db.init_db()
        from rwmod.tags import get_tags

        bundle = env.tmp / "v2.rwmod"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("profiles/alpha/A.xml", "<ModsConfig/>")
            zf.writestr("profiles/beta/B.xml", "<ModsConfig/>")
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {"app": "rwmod", "version": 2, "instances": [{"id": "alpha"}, {"id": "beta"}]}
                ),
            )
            zf.writestr(
                "tags.json",
                json.dumps({"version": 2, "instances": {"alpha": {"Mod": ["T"]}}}),
            )

        result = env.transfer.import_bundle(xfer, bundle)
        assert result["ok"] is True
        assert result["profiles"] == 2
        assert (env.profiles / "alpha" / "A.xml").exists()
        assert (env.profiles / "beta" / "B.xml").exists()
        assert get_tags("Mod", instance_id="alpha") == ["T"]
        assert get_tags("Mod", instance_id="beta") == []

    def test_mod_cache_memory_layer_is_partitioned_by_mods_dir(self, env, monkeypatch):
        env.db.init_db()
        dir_a = env.tmp / "ModsA"
        dir_b = env.tmp / "ModsB"
        _make_mod(dir_a, "Same", "Alpha")
        _make_mod(dir_b, "Same", "Beta")

        # Config.save() writes to the *actual* Config.CONFIG_PATH, so point it
        # at the file the cache layer will load before saving. Patching it
        # afterwards left instances.toml unwritten: resolve_instance_id() then
        # fell back to the active instance for both dirs, and whether dir_b
        # yielded "Beta" depended on the two "Same" folders having different
        # mtimes (equal mtimes served dir_a's cached "Alpha").
        cf = env.tmp / "instances.toml"
        monkeypatch.setattr(Config, "CONFIG_PATH", cf)
        cfg = Config(mods_dir=dir_a)
        cfg.add_instance(name="B", mods_dir=dir_b)
        cfg.save()
        Config._cache = None
        Config._cache_ts = 0.0
        Config._cache_path_key = None

        import rwmod.mod_cache as mod_cache

        mod_cache.clear_memory_cache()
        assert [m.name for m in mod_cache.get_cached_mods(dir_a)] == ["Alpha"]
        assert [m.name for m in mod_cache.get_cached_mods(dir_b)] == ["Beta"]
        # Switching back within the TTL must not serve the other instance's entry.
        assert [m.name for m in mod_cache.get_cached_mods(dir_a)] == ["Alpha"]


# ── cache_db.py / mod_cache.py ─────────────────────────────────────


class TestCacheLayers:
    def test_memory_layer_skips_rescan_within_ttl(self, env, monkeypatch):
        env.db.init_db()
        mods = env.tmp / "Mods"
        _make_mod(mods, "ModA", "Alpha")
        _make_mod(mods, "ModB", "Beta")

        import rwmod.cache_db as cache_db
        import rwmod.mod_cache as mod_cache

        real_refresh = cache_db.get_or_refresh_metas
        calls: list[int] = []

        def counting_refresh(d: Path):
            calls.append(1)
            return real_refresh(d)

        monkeypatch.setattr(cache_db, "get_or_refresh_metas", counting_refresh)
        mod_cache.invalidate_cache()

        first = mod_cache.get_cached_mods(mods)  # cold → layer 2
        assert sorted(m.name for m in first) == ["Alpha", "Beta"]
        assert len(calls) == 1

        second = mod_cache.get_cached_mods(mods)  # warm → layer 1, no rescan
        assert sorted(m.name for m in second) == ["Alpha", "Beta"]
        assert len(calls) == 1, "in-memory layer should skip the persistent scan"

        os.utime(mods / "ModA", (time.time() + 5, time.time() + 5))
        mod_cache.get_cached_mods(mods)  # child-dir mtime changed → layer 2 again
        assert len(calls) == 2

    def test_deleted_folder_disappears_from_both_layers(self, env):
        env.db.init_db()
        mods = env.tmp / "Mods"
        _make_mod(mods, "ModA", "Alpha")
        _make_mod(mods, "ModB", "Beta")

        import rwmod.mod_cache as mod_cache

        mod_cache.invalidate_cache()
        assert sorted(m.folder for m in mod_cache.get_cached_mods(mods)) == ["ModA", "ModB"]

        shutil.rmtree(mods / "ModB")
        # Windows file timestamps can tie at ~15ms granularity; force a distinct
        # parent mtime so this exercises propagation rather than the TTL window.
        later = time.time() + 10
        os.utime(mods, (later, later))
        assert sorted(m.folder for m in mod_cache.get_cached_mods(mods)) == ["ModA"]
        rows = env.db.get_conn().execute("SELECT folder FROM local_mod_metadata").fetchall()
        assert [r["folder"] for r in rows] == ["ModA"]

    def test_invalidate_folder_forces_reparse(self, env, monkeypatch):
        env.db.init_db()
        mods = env.tmp / "Mods"
        _make_mod(mods, "ModA", "Alpha")

        import rwmod.cache_db as cache_db
        import rwmod.mod_cache as mod_cache

        mod_cache.invalidate_cache()
        parsed: list[str] = []
        real = cache_db.read_mod_metadata

        def spy(path: Path):
            parsed.append(Path(path).name)
            return real(path)

        monkeypatch.setattr(cache_db, "read_mod_metadata", spy)
        mod_cache.get_cached_mods(mods)  # cold: parses once
        assert parsed == ["ModA"]

        mod_cache.invalidate_cache("ModA")
        assert sorted(m.folder for m in mod_cache.get_cached_mods(mods)) == ["ModA"]
        assert parsed == ["ModA", "ModA"]

    def test_missing_mods_dir_clears_state(self, env):
        env.db.init_db()
        import rwmod.mod_cache as mod_cache

        mod_cache.invalidate_cache()
        assert mod_cache.get_cached_mods(env.tmp / "Nope") == []
        assert mod_cache._cache == {}


# ── config.py ──────────────────────────────────────────────────────


class TestConfigCacheAndEscaping:
    def test_load_cache_ttl_and_save_invalidation(self, env):
        env.config_path.write_text('mods_dir = "E:/First"\n', encoding="utf-8")
        assert Config.load().mods_dir == Path("E:/First")

        env.config_path.write_text('mods_dir = "E:/Second"\n', encoding="utf-8")
        assert Config.load().mods_dir == Path("E:/First"), "TTL cache should still serve"

        Config._cache_ts = 0.0  # TTL elapsed
        assert Config.load().mods_dir == Path("E:/Second")

        cfg = Config(mods_dir=Path("E:/Third"))
        cfg.save()
        assert Config.load().mods_dir == Path("E:/Third")

    def test_save_escapes_quotes_and_backslashes(self, env):
        cfg = Config(
            steamcmd_path=env.tmp / "steamcmd.exe",
            mods_dir=env.tmp / "Mods",
            steam_api_key='we"ird\\key',
        )
        cfg.save()

        loaded = Config.load()
        assert loaded.steam_api_key == 'we"ird\\key'
        assert loaded.mods_dir == env.tmp / "Mods"

    def test_config_path_key_isolates_caches(self, env, tmp_path, monkeypatch):
        env.config_path.write_text('mods_dir = "E:/A"\n', encoding="utf-8")
        assert Config.load().mods_dir == Path("E:/A")

        other = tmp_path / "other.toml"
        other.write_text('mods_dir = "E:/B"\n', encoding="utf-8")
        monkeypatch.setattr(Config, "CONFIG_PATH", other)
        assert Config.load().mods_dir == Path("E:/B")


# ── tags.py concurrency ────────────────────────────────────────────


class TestTagsConcurrency:
    def test_add_tag_survives_a_concurrent_writer(self, env, monkeypatch):
        env.db.init_db()
        import rwmod.tags as tags_mod

        other = sqlite3.connect(str(env.db.DB_PATH))
        state = {"injected": False}
        real_get_conn = env.db.get_conn

        class RacingConnection:
            """Interposes one competing INSERT between add_tag's SELECT and INSERT."""

            def __init__(self, conn: sqlite3.Connection) -> None:
                self._conn = conn

            def execute(self, sql: str, *params: object):
                cur = self._conn.execute(sql, *params)
                if sql.lstrip().upper().startswith("SELECT") and not state["injected"]:
                    state["injected"] = True
                    other.execute("INSERT INTO mod_tags (folder, tag) VALUES (?, ?)", ("m", "T"))
                    other.commit()
                return cur

            def commit(self) -> None:
                self._conn.commit()

        monkeypatch.setattr(tags_mod, "get_conn", lambda: RacingConnection(real_get_conn()))

        try:
            added = tags_mod.add_tag("m", "T")  # the loser must just report "already there"
        except sqlite3.IntegrityError as exc:
            pytest.fail(f"add_tag raised IntegrityError on a concurrent duplicate: {exc}")
        finally:
            other.close()
        assert added is False
        assert tags_mod.get_tags("m") == ["T"]
