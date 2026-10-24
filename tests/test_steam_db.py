"""Tests for the opt-in Steam Workshop database (steam_db.py).

The real database is ~47 MB / 57k entries, so every test uses a local sample
payload. Network access is monkeypatched; the one live-download test carries
``@pytest.mark.network``. Every cache path is redirected under ``tmp_path``
(``_isolate_user_paths`` in conftest patches ``Path.home`` for all tests).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from rwmod.config import Config
from rwmod.steam_db import CACHE_FILENAME, CACHE_TTL, MAX_CACHE_BYTES, STEAM_DB_URL, SteamDB

SAMPLE_DB = {
    "version": 1790337009,
    "database": {
        "2009463077": {
            "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077",
            "steamName": "Harmony",
            "packageId": "brrainz.harmony",
            "name": "Harmony",
            "authors": "Andreas Pardeike",
            "dependencies": {
                "294100": ["RimWorld", "https://store.steampowered.com/app/294100"],
            },
            "gameVersions": ["1.4", "1.5", "1.6"],
            "tags": [
                {"tag": "1.5", "display_name": "1.5"},
                {"tag": "Mod", "display_name": "Mod"},
            ],
        },
        "1234567890": {
            "steamName": "Old Mod",
            "packageId": "old.mod",
            "name": "Old Mod",
            "authors": "Someone",
            "gameVersions": ["1.5"],
            "tags": [],
        },
        "999": {"steamName": "Nameless", "name": "Nameless"},
        "bad-entry": "not a dict",
    },
}


def _sample_bytes() -> bytes:
    return json.dumps(SAMPLE_DB, ensure_ascii=False).encode("utf-8")


def _write_cache(directory: Path, payload: object = SAMPLE_DB) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CACHE_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _FakeResponse:
    def __init__(self, payload: bytes = b"", *, status: int = 200, etag: str = '"new"'):
        self._payload = payload
        self.status = status
        self.headers = {"ETag": etag}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


@pytest.fixture(autouse=True)
def _reset_singleton():
    SteamDB.reset()
    yield
    SteamDB.reset()


def _no_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("network attempted")


class TestCacheContract:
    def test_missing_cache_is_offline_and_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        monkeypatch.setattr(sd_mod, "urlopen", _no_network)
        db = SteamDB(cache_dir=tmp_path)
        assert db.load_cached() is False
        assert db.query("2009463077") is None
        assert db.query_package("brrainz.harmony") is None

    def test_load_cached_exposes_query_fields(self, tmp_path: Path):
        _write_cache(tmp_path)
        db = SteamDB(cache_dir=tmp_path)

        assert db.load_cached() is True
        info = db.query("2009463077")
        assert info is not None
        assert info["name"] == "Harmony"
        assert info["package_id"] == "brrainz.harmony"
        assert info["authors"] == "Andreas Pardeike"
        assert info["game_versions"] == ["1.4", "1.5", "1.6"]
        assert info["dependencies"] == {"294100": "RimWorld"}
        assert info["tags"] == ["1.5", "Mod"]
        assert db.query("does-not-exist") is None

    def test_query_by_package_id_is_case_insensitive(self, tmp_path: Path):
        _write_cache(tmp_path)
        db = SteamDB(cache_dir=tmp_path)

        assert db.load_cached() is True
        assert db.query_package("  BRRainz.Harmony  ")["workshop_id"] == "2009463077"
        assert db.query_package("old.mod")["workshop_id"] == "1234567890"
        assert db.query_package("nope.mod") is None

    def test_entries_without_package_id_are_still_queryable(self, tmp_path: Path):
        _write_cache(tmp_path)
        db = SteamDB(cache_dir=tmp_path)

        assert db.load_cached() is True
        assert db.query("999")["package_id"] == ""
        assert db.query_package("") is None

    def test_malformed_entries_are_ignored(self, tmp_path: Path):
        _write_cache(tmp_path)
        db = SteamDB(cache_dir=tmp_path)

        assert db.load_cached() is True
        assert db.query("bad-entry") is None
        assert db.status()["entries"] == 3

    def test_stale_cache_is_still_used_for_queries(self, tmp_path: Path):
        path = _write_cache(tmp_path)
        old = time.time() - (CACHE_TTL + 3600)
        os.utime(path, (old, old))
        db = SteamDB(cache_dir=tmp_path)

        assert db.load_cached() is True
        assert db.query("2009463077") is not None
        status = db.status()
        assert status["stale"] is True
        assert status["valid"] is True

    def test_fresh_cache_is_not_stale(self, tmp_path: Path):
        _write_cache(tmp_path)
        assert SteamDB(cache_dir=tmp_path).status()["stale"] is False

    def test_oversized_cache_is_rejected(self, tmp_path: Path):
        path = _write_cache(tmp_path)
        db = SteamDB(cache_dir=tmp_path, max_bytes=32)
        assert path.stat().st_size > 32

        assert db.load_cached() is False
        assert db.query("2009463077") is None
        status = db.status()
        assert status["cached"] is True
        assert status["valid"] is False
        assert status["entries"] == 0

    def test_corrupt_cache_is_fail_open(self, tmp_path: Path):
        (tmp_path / CACHE_FILENAME).write_text("{not json", encoding="utf-8")
        db = SteamDB(cache_dir=tmp_path)

        assert db.load_cached() is False
        assert db.query("2009463077") is None
        status = db.status()
        assert status["cached"] is True
        assert status["valid"] is False

    def test_payload_without_database_is_rejected(self, tmp_path: Path):
        _write_cache(tmp_path, {"version": 1, "database": []})
        db = SteamDB(cache_dir=tmp_path)

        assert db.load_cached() is False
        assert db.status()["valid"] is False

    def test_cache_is_parsed_at_most_once(self, tmp_path: Path):
        path = _write_cache(tmp_path)
        db = SteamDB(cache_dir=tmp_path)
        assert db.load_cached() is True

        # A second load must reuse the in-memory index: with the file gone, a
        # re-read would fail and every query would return None.
        path.unlink()
        assert db.load_cached() is True
        assert db.query("2009463077") is not None
        assert db.query_package("brrainz.harmony") is not None

    def test_default_cache_dir_follows_home(self):
        db = SteamDB()
        assert db.cache_path == Path.home() / ".rwmod" / CACHE_FILENAME

    def test_get_rebuilds_when_home_changes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        first = SteamDB.get()
        other_home = tmp_path / "other-home"
        other_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: other_home))

        second = SteamDB.get()
        assert second is not first
        assert second.cache_path == other_home / ".rwmod" / CACHE_FILENAME
        assert SteamDB.get() is second


class TestRefresh:
    def test_refresh_downloads_writes_cache_and_etag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        monkeypatch.setattr(
            sd_mod, "urlopen", lambda req, timeout=30: _FakeResponse(_sample_bytes())
        )
        db = SteamDB(cache_dir=tmp_path / "cache")

        assert db.refresh() is True
        cache = tmp_path / "cache" / CACHE_FILENAME
        assert cache.exists()
        assert json.loads(cache.read_text(encoding="utf-8"))["version"] == 1790337009
        assert (tmp_path / "cache" / "steam_db.etag").read_text(encoding="utf-8") == '"new"'
        assert not list((tmp_path / "cache").glob("*.tmp"))
        assert db.query("2009463077")["name"] == "Harmony"
        status = db.status()
        assert status["valid"] is True
        assert status["entries"] == 3
        assert status["version"] == 1790337009
        assert status["size"] == cache.stat().st_size

    def test_refresh_sends_if_none_match_and_304_refreshes_ttl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from urllib.error import HTTPError

        from rwmod import steam_db as sd_mod

        path = _write_cache(tmp_path)
        old = time.time() - (CACHE_TTL + 3600)
        os.utime(path, (old, old))
        (tmp_path / "steam_db.etag").write_text('"abc"', encoding="utf-8")
        seen: dict[str, str | None] = {}

        def fake(req, timeout=30):
            seen["if_none_match"] = req.get_header("If-none-match")
            raise HTTPError(req.full_url, 304, "Not Modified", {}, None)

        monkeypatch.setattr(sd_mod, "urlopen", fake)
        db = SteamDB(cache_dir=tmp_path)

        assert db.refresh() is True
        assert seen["if_none_match"] == '"abc"'
        assert time.time() - path.stat().st_mtime < 60
        assert db.query("2009463077") is not None

    def test_refresh_response_status_304_is_handled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        _write_cache(tmp_path)
        monkeypatch.setattr(sd_mod, "urlopen", lambda req, timeout=30: _FakeResponse(status=304))
        assert SteamDB(cache_dir=tmp_path).refresh() is True

    def test_304_without_cache_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import steam_db as sd_mod

        monkeypatch.setattr(sd_mod, "urlopen", lambda req, timeout=30: _FakeResponse(status=304))
        assert SteamDB(cache_dir=tmp_path).refresh() is False

    def test_refresh_rejects_oversized_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        monkeypatch.setattr(
            sd_mod, "urlopen", lambda req, timeout=30: _FakeResponse(_sample_bytes())
        )
        db = SteamDB(cache_dir=tmp_path)

        assert db.refresh(max_bytes=32) is False
        assert not (tmp_path / CACHE_FILENAME).exists()

    def test_refresh_rejects_invalid_schema(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import steam_db as sd_mod

        monkeypatch.setattr(
            sd_mod, "urlopen", lambda req, timeout=30: _FakeResponse(b'{"database": []}')
        )
        assert SteamDB(cache_dir=tmp_path).refresh() is False
        assert not (tmp_path / CACHE_FILENAME).exists()

    def test_network_failure_keeps_stale_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        _write_cache(tmp_path)

        def boom(req, timeout=30):
            raise OSError("offline")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        db = SteamDB(cache_dir=tmp_path)

        assert db.refresh() is False
        assert db.query("2009463077") is not None

    def test_http_error_is_fail_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from urllib.error import HTTPError

        from rwmod import steam_db as sd_mod

        def boom(req, timeout=30):
            raise HTTPError(req.full_url, 500, "Server Error", {}, None)

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        assert SteamDB(cache_dir=tmp_path).refresh() is False

    def test_failed_refresh_leaves_no_partial_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import steam_db as sd_mod

        def boom(req, timeout=30):
            raise OSError("offline")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        assert SteamDB(cache_dir=tmp_path).refresh() is False
        assert not list(tmp_path.glob("*"))


class TestStatus:
    def test_missing_cache_status(self, tmp_path: Path):
        status = SteamDB(cache_dir=tmp_path, max_bytes=1234).status()
        assert status["cached"] is False
        assert status["valid"] is False
        assert status["entries"] == 0
        assert status["version"] is None
        assert status["updated_at"] is None
        assert status["size"] == 0
        assert status["stale"] is False
        assert status["max_bytes"] == 1234
        assert status["ttl_seconds"] == CACHE_TTL

    def test_status_reports_timestamp_and_size(self, tmp_path: Path):
        path = _write_cache(tmp_path)
        status = SteamDB(cache_dir=tmp_path).status()

        assert status["cached"] is True
        assert status["valid"] is True
        assert status["entries"] == 3
        assert status["size"] == path.stat().st_size
        assert status["updated_at"] is not None
        parsed = datetime.fromisoformat(status["updated_at"])
        assert parsed.tzinfo is not None
        assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60

    def test_stale_flag_uses_30_day_ttl(self, tmp_path: Path):
        path = _write_cache(tmp_path)
        past = datetime.now(UTC) - timedelta(days=31)
        os.utime(path, (past.timestamp(), past.timestamp()))
        status = SteamDB(cache_dir=tmp_path).status()
        assert status["stale"] is True

    def test_status_never_downloads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import steam_db as sd_mod

        monkeypatch.setattr(sd_mod, "urlopen", _no_network)
        _write_cache(tmp_path)
        assert SteamDB(cache_dir=tmp_path).status()["valid"] is True


class TestConstants:
    def test_cache_contract_values(self):
        assert CACHE_FILENAME == "steam_db.json"
        assert CACHE_TTL == 30 * 24 * 3600
        assert MAX_CACHE_BYTES == 60 * 1024 * 1024
        assert STEAM_DB_URL.startswith("https://raw.githubusercontent.com/RimSort/")

    def test_default_max_bytes_used_by_status(self, tmp_path: Path):
        assert SteamDB(cache_dir=tmp_path).status()["max_bytes"] == MAX_CACHE_BYTES


class TestConfigFields:
    def test_defaults_are_off(self):
        cfg = Config()
        assert cfg.enable_steam_db is False
        assert cfg.steam_db_max_bytes == MAX_CACHE_BYTES

    def test_load_reads_fields(self, tmp_path: Path):
        f = tmp_path / ".rwmod.toml"
        f.write_text(
            'steamcmd_path = "D:/steamcmd/steamcmd.exe"\n'
            'mods_dir = "E:/MyMods"\n'
            "enable_steam_db = true\n"
            "steam_db_max_bytes = 123456\n",
            encoding="utf-8",
        )
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config.load()
        assert cfg.enable_steam_db is True
        assert cfg.steam_db_max_bytes == 123456

    def test_old_config_defaults_off(self, tmp_path: Path):
        f = tmp_path / ".rwmod.toml"
        f.write_text(
            'steamcmd_path = "D:/steamcmd/steamcmd.exe"\nmods_dir = "E:/MyMods"\n',
            encoding="utf-8",
        )
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config.load()
        assert cfg.enable_steam_db is False
        assert cfg.steam_db_max_bytes == MAX_CACHE_BYTES

    def test_save_round_trips_non_defaults(self, tmp_path: Path):
        f = tmp_path / "roundtrip.toml"
        with patch.object(Config, "CONFIG_PATH", f):
            cfg = Config(enable_steam_db=True, steam_db_max_bytes=4096)
            cfg.save()
            text = f.read_text(encoding="utf-8")
            assert "enable_steam_db = true" in text
            assert "steam_db_max_bytes = 4096" in text
            loaded = Config.load()
        assert loaded.enable_steam_db is True
        assert loaded.steam_db_max_bytes == 4096

    def test_save_omits_defaults(self, tmp_path: Path):
        f = tmp_path / "defaults.toml"
        with patch.object(Config, "CONFIG_PATH", f):
            Config().save()
            text = f.read_text(encoding="utf-8")
        assert "enable_steam_db" not in text
        assert "steam_db_max_bytes" not in text


@pytest.mark.network
def test_live_download_shape(tmp_path: Path):
    """Real 47 MB download — opt-in via ``-m network`` only."""
    db = SteamDB(cache_dir=tmp_path)
    assert db.refresh() is True
    status = db.status()
    assert status["entries"] > 50_000
    assert db.query("2009463077") is not None
