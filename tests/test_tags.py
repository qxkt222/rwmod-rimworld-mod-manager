"""Tests for tags.py \u2014 mod tag/category management."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.database import close_db, init_db
from rwmod.tags import (
    add_tag,
    get_mods_by_tag,
    get_tags,
    list_all_tags,
    remove_all_tags,
    remove_tag,
)


@pytest.fixture
def temp_db_tags(tmp_path: Path):
    db_path = tmp_path / "test_tags.db"
    with patch("rwmod.database.DB_PATH", db_path):
        init_db()
        yield
        close_db()
    db_path.unlink(missing_ok=True)


class TestAddTag:
    def test_add_new_tag(self, temp_db_tags):
        assert add_tag("mod_folder", "QoL")
        assert "QoL" in get_tags("mod_folder")

    def test_add_duplicate(self, temp_db_tags):
        add_tag("mod_folder", "QoL")
        assert not add_tag("mod_folder", "QoL")

    def test_add_multiple_tags(self, temp_db_tags):
        add_tag("mod1", "QoL")
        add_tag("mod1", "Content")
        tags = get_tags("mod1")
        assert len(tags) == 2
        assert "QoL" in tags
        assert "Content" in tags

    def test_add_strips_whitespace(self, temp_db_tags):
        add_tag("mod_folder", "  QoL  ")
        assert "QoL" in get_tags("mod_folder")
        assert "  QoL  " not in get_tags("mod_folder")


class TestRemoveTag:
    def test_remove_existing(self, temp_db_tags):
        add_tag("mod_folder", "QoL")
        assert remove_tag("mod_folder", "QoL")
        assert get_tags("mod_folder") == []

    def test_remove_nonexistent(self, temp_db_tags):
        assert not remove_tag("mod_folder", "nonexistent")


class TestGetModsByTag:
    def test_returns_folders(self, temp_db_tags):
        add_tag("mod_a", "QoL")
        add_tag("mod_b", "QoL")
        add_tag("mod_c", "Content")
        qol = get_mods_by_tag("QoL")
        assert sorted(qol) == ["mod_a", "mod_b"]


class TestListAllTags:
    def test_returns_with_counts(self, temp_db_tags):
        add_tag("a", "QoL")
        add_tag("b", "QoL")
        add_tag("c", "Content")
        all_tags = list_all_tags()
        assert len(all_tags) == 2
        counts = {t["tag"]: t["count"] for t in all_tags}
        assert counts["QoL"] == 2
        assert counts["Content"] == 1

    def test_empty(self, temp_db_tags):
        assert list_all_tags() == []


class TestRemoveAllTags:
    def test_removes_all(self, temp_db_tags):
        add_tag("mod", "QoL")
        add_tag("mod", "Content")
        count = remove_all_tags("mod")
        assert count == 2
        assert get_tags("mod") == []

    def test_nonexistent_folder(self, temp_db_tags):
        assert remove_all_tags("nonexistent") == 0


class TestInstanceScopedTags:
    """Wave 6a: two instances with the same folder name keep separate tags."""

    def test_same_folder_is_isolated_per_instance(self, temp_db_tags):
        assert add_tag("Shared", "QoL", instance_id="alpha")
        assert add_tag("Shared", "Content", instance_id="beta")

        assert get_tags("Shared", instance_id="alpha") == ["QoL"]
        assert get_tags("Shared", instance_id="beta") == ["Content"]
        assert get_mods_by_tag("QoL", instance_id="beta") == []
        assert get_mods_by_tag("Content", instance_id="alpha") == []

        assert list_all_tags(instance_id="alpha") == [{"tag": "QoL", "count": 1}]
        assert list_all_tags(instance_id="beta") == [{"tag": "Content", "count": 1}]

        assert remove_tag("Shared", "QoL", instance_id="beta") is False
        assert remove_tag("Shared", "QoL", instance_id="alpha") is True

        remove_all_tags("Shared", instance_id="beta")
        assert get_tags("Shared", instance_id="beta") == []

    def test_omitted_instance_uses_the_default(self, temp_db_tags):
        add_tag("mod", "QoL")
        assert get_tags("mod", instance_id="default") == ["QoL"]
        assert list_all_tags(instance_id="default") == [{"tag": "QoL", "count": 1}]
