"""Test workshop — data model and logic, no network calls.

Only tests logic paths that don't require network:
ModSearchResult dataclass, fetch_item_dependencies with empty input, plus
collection/child parsing against mocked Steam API responses.
"""

from __future__ import annotations

import json
from pathlib import Path

from rwmod import workshop
from rwmod.workshop import (
    ModSearchResult,
    _dedup_ids,
    _fetch_collection_api,
    fetch_collection_children,
    fetch_item_dependencies,
    is_collection,
)


class _FakeResp:
    """Context-manager response object whose read() returns canned JSON bytes."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


class _FakeOpener:
    """Replaces _shared_opener.open(); serves one canned JSON payload per call."""

    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = list(payloads)

    def open(self, req: object, timeout: int = 15) -> _FakeResp:
        del req, timeout  # canned — the request content is not inspected
        data = self._payloads.pop(0)
        return _FakeResp(json.dumps(data).encode("utf-8"))


_COLLECTION_DETAILS = {
    "response": {
        "result": 1,
        "resultcount": 1,
        "collectiondetails": [
            {
                "publishedfileid": "3760470751",
                "result": 1,
                "children": [
                    {"publishedfileid": "111", "sortorder": 1, "filetype": 0},
                    {"publishedfileid": "222", "sortorder": 2, "filetype": 0},
                    {"publishedfileid": "333", "sortorder": 3, "filetype": 0},
                ],
            }
        ],
    }
}

_NON_COLLECTION_DETAILS = {
    "response": {
        "result": 1,
        "resultcount": 0,
        "collectiondetails": [{"publishedfileid": "2009463077", "result": 9}],
    }
}


class TestModSearchResult:
    def test_defaults(self):
        r = ModSearchResult(id="123", title="Test", author="Author")
        assert r.id == "123"
        assert r.title == "Test"
        assert r.author == "Author"
        assert r.description == ""
        assert r.preview_url == ""

    def test_full_fields(self):
        r = ModSearchResult(
            id="123",
            title="Test Mod",
            author="Tester",
            description="A great mod",
            preview_url="https://img.example.com/thumb.png",
            rating="4.5",
            subscribers="10000",
        )
        assert r.description == "A great mod"
        assert r.preview_url == "https://img.example.com/thumb.png"
        assert r.rating == "4.5"
        assert r.subscribers == "10000"

    def test_dataclass_equality(self):
        """ModSearchResult supports field comparison."""
        a = ModSearchResult(id="1", title="A", author="X")
        b = ModSearchResult(id="1", title="A", author="X")
        assert a == b
        c = ModSearchResult(id="2", title="A", author="X")
        assert a != c


class TestIsCollection:
    """is_collection uses GetCollectionDetails — result==1 means collection."""

    def test_collection_true(self, monkeypatch):
        monkeypatch.setattr(workshop, "_shared_opener", _FakeOpener([_COLLECTION_DETAILS]))
        assert is_collection("3760470751") is True

    def test_non_collection_false(self, monkeypatch):
        monkeypatch.setattr(workshop, "_shared_opener", _FakeOpener([_NON_COLLECTION_DETAILS]))
        assert is_collection("2009463077") is False

    def test_network_failure_false(self, monkeypatch):
        class Boom:
            def open(self, req: object, timeout: int = 15) -> object:
                del req, timeout
                raise OSError("no network")

        monkeypatch.setattr(workshop, "_shared_opener", Boom())
        assert is_collection("123") is False


class TestFetchCollectionApi:
    """_fetch_collection_api parses children from GetCollectionDetails."""

    def test_parses_children(self, monkeypatch):
        monkeypatch.setattr(workshop, "_shared_opener", _FakeOpener([_COLLECTION_DETAILS]))
        assert _fetch_collection_api("3760470751") == ["111", "222", "333"]

    def test_non_collection_empty(self, monkeypatch):
        monkeypatch.setattr(workshop, "_shared_opener", _FakeOpener([_NON_COLLECTION_DETAILS]))
        assert _fetch_collection_api("2009463077") == []

    def test_empty_collection_returns_empty(self, monkeypatch):
        payload = {
            "response": {
                "result": 1,
                "resultcount": 1,
                "collectiondetails": [{"publishedfileid": "x", "result": 1, "children": []}],
            }
        }
        monkeypatch.setattr(workshop, "_shared_opener", _FakeOpener([payload]))
        assert _fetch_collection_api("x") == []


class TestFetchCollectionChildren:
    """Children must never be missed — API and scrape results are unioned."""

    def test_api_only(self, monkeypatch):
        monkeypatch.setattr(
            workshop, "_fetch_collection_api", lambda cid, api_key="anonymous": ["111", "222"]
        )
        monkeypatch.setattr(workshop, "_scrape_collection_page", lambda cid, timeout=30: [])
        assert fetch_collection_children("3760470751") == ["111", "222"]

    def test_union_api_and_scrape_dedup(self, monkeypatch):
        monkeypatch.setattr(
            workshop, "_fetch_collection_api", lambda cid, api_key="anonymous": ["111", "222"]
        )
        monkeypatch.setattr(
            workshop, "_scrape_collection_page", lambda cid, timeout=30: ["222", "333"]
        )
        # API order first; scrape-only children appended; duplicates dropped.
        assert fetch_collection_children("3760470751") == ["111", "222", "333"]

    def test_scrape_fallback_when_api_empty(self, monkeypatch):
        monkeypatch.setattr(workshop, "_fetch_collection_api", lambda cid, api_key="anonymous": [])
        monkeypatch.setattr(
            workshop, "_scrape_collection_page", lambda cid, timeout=30: ["111", "222"]
        )
        assert fetch_collection_children("3760470751") == ["111", "222"]

    def test_all_methods_fail_returns_empty(self, monkeypatch):
        monkeypatch.setattr(workshop, "_fetch_collection_api", lambda cid, api_key="anonymous": [])
        monkeypatch.setattr(workshop, "_scrape_collection_page", lambda cid, timeout=30: [])
        assert fetch_collection_children("3760470751") == []


class TestDedupIds:
    def test_preserves_order(self):
        assert _dedup_ids(["2", "1", "2", "3", "1"]) == ["2", "1", "3"]

    def test_empty(self):
        assert _dedup_ids([]) == []


class TestFetchItemDependencies:
    def test_empty_list(self):
        assert fetch_item_dependencies([]) == {}

    def test_parses_children_as_deps(self, monkeypatch):
        payload = {
            "response": {
                "result": 1,
                "resultcount": 1,
                "publishedfiledetails": [
                    {
                        "publishedfileid": "100",
                        "title": "Some Mod",
                        "children": [{"publishedfileid": "200", "filetype": 0}],
                    }
                ],
            }
        }
        monkeypatch.setattr(workshop, "_shared_opener", _FakeOpener([payload]))
        assert fetch_item_dependencies(["100"]) == {"100": ["200"]}

    def test_no_children_no_deps(self, monkeypatch):
        payload = {
            "response": {
                "result": 1,
                "resultcount": 1,
                "publishedfiledetails": [{"publishedfileid": "100", "title": "X"}],
            }
        }
        monkeypatch.setattr(workshop, "_shared_opener", _FakeOpener([payload]))
        assert fetch_item_dependencies(["100"]) == {}


class TestCheckModUpdates:
    """check_mod_updates with the network layer mocked — focuses on the
    .rwmod_last_updated → DB migration and timestamp comparison."""

    def _make_mod(self, tmp_path: Path, wid: str = "100", name: str = "Some Mod"):
        mod = tmp_path / "Mods" / name
        about = mod / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            f"<ModMetaData><name>{name}</name><packageId>author.{name.lower()}</packageId></ModMetaData>"
        )
        (about / "PublishedFileId.txt").write_text(wid)
        return mod

    def _mock_remote(self, monkeypatch, time_updated: int):
        monkeypatch.setattr(
            workshop,
            "_fetch_batch_parallel",
            lambda ids: {wid: {"time_updated": time_updated, "title": "Remote"} for wid in ids},
        )

    def test_legacy_marker_file_is_migrated_to_db(self, tmp_path: Path, monkeypatch):
        """An existing .rwmod_last_updated marker must seed the DB timestamp
        (and the marker file is cleaned up) instead of flagging every mod as
        updated on the first post-upgrade check."""
        from unittest.mock import patch as _patch

        from rwmod.database import close_db, get_last_updated, init_db

        mod = self._make_mod(tmp_path)
        # Legacy marker: mod checked "yesterday" (old), remote is newer.
        old_ts = int(__import__("time").time()) - 86400
        (mod / ".rwmod_last_updated").write_text(str(old_ts))

        db_path = tmp_path / "updates.db"
        with _patch("rwmod.database.DB_PATH", db_path):
            init_db()
            self._mock_remote(monkeypatch, old_ts + 3600)  # remote is newer

            updates = workshop.check_mod_updates(str(tmp_path / "Mods"))

            # Mod is correctly reported as outdated AND the marker migrated.
            assert len(updates) == 1
            assert updates[0]["workshop_id"] == "100"
            assert not (mod / ".rwmod_last_updated").exists()
            assert get_last_updated("Some Mod") == old_ts
        close_db()

    def test_up_to_date_not_reported(self, tmp_path: Path, monkeypatch):
        from unittest.mock import patch as _patch

        from rwmod.database import close_db, init_db

        self._make_mod(tmp_path)
        db_path = tmp_path / "updates.db"
        with _patch("rwmod.database.DB_PATH", db_path):
            init_db()
            now = int(__import__("time").time())
            self._mock_remote(monkeypatch, now - 100)  # remote older than install
            # Seed the DB timestamp via a fresh check (no marker file).
            workshop.check_mod_updates(str(tmp_path / "Mods"))
            updates = workshop.check_mod_updates(str(tmp_path / "Mods"))
            assert updates == []
        close_db()
