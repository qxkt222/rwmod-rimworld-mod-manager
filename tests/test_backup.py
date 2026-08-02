"""Tests for backup.py — zip-based mod backup/restore."""

from __future__ import annotations

import zipfile
from pathlib import Path

from rwmod.backup import backup_mod, delete_backup, list_backups, restore_mod


class TestBackupMod:
    def test_backup_creates_zip(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        mod_dir = mods_dir / "test_mod"
        mod_dir.mkdir()
        (mod_dir / "About").mkdir()
        (mod_dir / "About" / "About.xml").write_text("<ModMetaData><name>Test</name></ModMetaData>")

        backup_dir = tmp_path / "backups"
        zip_path = backup_mod(mods_dir, "123", "test_mod", backup_dir)

        assert zip_path is not None
        assert zip_path.exists()
        assert zip_path.suffix == ".zip"
        assert "123__" in zip_path.name
        assert zipfile.is_zipfile(zip_path)

    def test_backup_missing_mod_returns_none(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        backup_dir = tmp_path / "backups"

        result = backup_mod(mods_dir, "999", "nonexistent", backup_dir)
        assert result is None

    def test_restore_latest_backup(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        mod_dir = mods_dir / "test_mod"
        mod_dir.mkdir()
        (mod_dir / "About").mkdir()
        (mod_dir / "About" / "About.xml").write_text(
            "<ModMetaData><name>Original</name></ModMetaData>"
        )

        backup_dir = tmp_path / "backups"
        backup_mod(mods_dir, "123", "test_mod", backup_dir)

        # Simulate update: delete old version
        import shutil

        shutil.rmtree(mod_dir)

        result = restore_mod(mods_dir, "123", backup_dir)
        assert result["ok"]
        assert (mods_dir / "test_mod" / "About" / "About.xml").exists()

    def test_restore_nonexistent_backup(self, tmp_path: Path):
        result = restore_mod(tmp_path / "Mods", "000", tmp_path / "backups")
        assert not result["ok"]

    def test_list_backups(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        mod_dir = mods_dir / "my_mod"
        mod_dir.mkdir()
        (mod_dir / "About").mkdir()
        (mod_dir / "About" / "About.xml").write_text("<ModMetaData/>")

        backup_dir = tmp_path / "backups"
        backup_mod(mods_dir, "123", "my_mod", backup_dir)

        backups = list_backups(backup_dir)
        assert len(backups) == 1
        assert backups[0]["workshop_id"] == "123"
        assert backups[0]["folder_name"] == "my_mod"

    def test_delete_backup(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        mod_dir = mods_dir / "mod"
        mod_dir.mkdir()
        (mod_dir / "About").mkdir()
        (mod_dir / "About" / "About.xml").write_text("<ModMetaData/>")

        backup_dir = tmp_path / "backups"
        zip_path = backup_mod(mods_dir, "456", "mod", backup_dir)
        assert zip_path is not None

        ok = delete_backup(backup_dir, zip_path.name)
        assert ok
        assert not zip_path.exists()


class TestBackupPathTraversal:
    def test_delete_rejects_traversal(self, tmp_path: Path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        victim = tmp_path / "victim.zip"
        victim.write_bytes(b"data")

        assert delete_backup(backup_dir, "..\\victim.zip") is False
        assert delete_backup(backup_dir, "../victim.zip") is False
        assert delete_backup(backup_dir, "sub/victim.zip") is False
        assert victim.exists()

    def test_restore_rejects_traversal_filename(self, tmp_path: Path):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        result = restore_mod(tmp_path / "Mods", "123", backup_dir, backup_filename="..\\evil.zip")
        assert not result["ok"]

    def test_restore_rejects_zip_path_traversal(self, tmp_path: Path):
        """A backup zip containing ../ entries must not escape mods_dir."""
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        evil = backup_dir / "123__mod__20240101_000000.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../evil.txt", "boom")

        result = restore_mod(mods_dir, "123", backup_dir)
        assert not result["ok"]
        assert not (tmp_path / "evil.txt").exists()

    def test_restore_accepts_plain_filename(self, tmp_path: Path):
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        mod_dir = mods_dir / "my_mod"
        mod_dir.mkdir()
        (mod_dir / "About").mkdir()
        (mod_dir / "About" / "About.xml").write_text("<ModMetaData/>")

        backup_dir = tmp_path / "backups"
        zip_path = backup_mod(mods_dir, "123", "my_mod", backup_dir)
        assert zip_path is not None
        import shutil

        shutil.rmtree(mod_dir)

        result = restore_mod(mods_dir, "123", backup_dir, backup_filename=zip_path.name)
        assert result["ok"]
        assert (mods_dir / "my_mod" / "About" / "About.xml").exists()

    def test_restore_sanitized_folder_name(self, tmp_path: Path):
        """Restore must work even when the folder name in the backup filename
        differs from the actual top-level directory inside the zip.

        The zip stores the *original* folder name, so restore must detect the
        top-level directory from the archive contents rather than trusting the
        (possibly sanitized) name parsed from the filename.
        """
        mods_dir = tmp_path / "Mods"
        mods_dir.mkdir()
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Filename claims the folder is "sanitized", but the zip actually
        # contains a top-level dir named "OriginalName" — restore must use the
        # real name from the archive, not the one in the filename.
        zip_path = backup_dir / "123__sanitized__20240101_000000.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("OriginalName/About/About.xml", "<ModMetaData/>")

        result = restore_mod(mods_dir, "123", backup_dir)
        assert result["ok"]
        assert (mods_dir / "OriginalName" / "About" / "About.xml").exists()
