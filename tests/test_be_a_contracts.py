"""Wave-1 backend contracts owned by BE-A (API / safety / data layer).

Everything here exercises public behaviour: HTTP status codes and bodies,
on-disk results a user can observe, and the documented failure contract from
CLAUDE.md. No private helper is asserted on unless it is the module-level
contract other modules import.
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rwmod.config import Config, current_instance_id
from rwmod.models.schemas import QueueStateResponse
from rwmod.queue import DownloadQueue, QueueItem
from rwmod.server import JsonBodyLimitMiddleware


@pytest.fixture
def cfg(client: TestClient) -> Config:  # noqa: ARG001 — depends on the isolated client
    """The config the isolated TestClient is serving (same isolated paths)."""
    return Config.load()


def _make_mod(mods_dir: Path, folder: str, package_id: str, workshop_id: str = "") -> None:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{folder}</name><packageId>{package_id}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    if workshop_id:
        (about / "PublishedFileId.txt").write_text(workshop_id, encoding="utf-8")


# ── 1. steamcmd_path must be a real steamcmd binary ────────────────


class TestSteamcmdPathValidation:
    def test_rejects_arbitrary_executable(self, client: TestClient, tmp_path: Path):
        evil = tmp_path / "evil.exe"
        evil.write_bytes(b"MZ")

        resp = client.post("/api/config", json={"steamcmd_path": str(evil)})

        assert resp.status_code == 400
        assert "steamcmd" in resp.json()["detail"].lower()

    def test_rejects_executable_inside_mods_dir(
        self, client: TestClient, cfg: Config, tmp_path: Path
    ):
        """The upload→configure→execute chain: import-local can write here."""
        planted = cfg.mods_dir / "planted" / "steamcmd.exe"
        planted.parent.mkdir(parents=True)
        planted.write_bytes(b"MZ")

        resp = client.post("/api/config", json={"steamcmd_path": str(planted)})

        assert resp.status_code == 400
        assert "上传目录" in resp.json()["detail"]

    def test_accepts_steamcmd_basename(self, client: TestClient, cfg: Config):
        resp = client.post("/api/config", json={"steamcmd_path": str(cfg.steamcmd_path)})
        assert resp.status_code == 200, resp.text

    def test_config_validate_refuses_an_executable_planted_in_mods_dir(self, tmp_path: Path):
        """The download path spawns cfg.steamcmd_path — validate() is the last gate."""
        from rwmod.config import Config as Cfg
        from rwmod.errors import ConfigError

        mods = tmp_path / "Mods"
        mods.mkdir()
        planted = mods / "evil.exe"
        planted.write_bytes(b"MZ")
        cfg = Cfg(steamcmd_path=planted, mods_dir=mods)

        with pytest.raises(ConfigError):
            cfg.validate()

    def test_check_endpoint_refuses_non_steamcmd_without_executing(
        self, client: TestClient, cfg: Config, tmp_path: Path
    ):
        """A hand-edited TOML must not turn /api/steamcmd/check into an exec."""
        evil = tmp_path / "evil.exe"
        evil.write_bytes(b"MZ")
        cfg.steamcmd_path = evil
        cfg.save()

        resp = client.get("/api/steamcmd/check")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "拒绝执行" in body["msg"]


# ── 2. JSON body cap ───────────────────────────────────────────────


class TestJsonBodyLimit:
    @staticmethod
    def _scope(content_type: bytes) -> dict[str, Any]:
        return {"type": "http", "headers": [(b"content-type", content_type)]}

    def test_chunked_json_body_over_cap_is_rejected(self):
        reached: list[bytes] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            reached.append((await receive())["body"])

        middleware = JsonBodyLimitMiddleware(app, max_bytes=8)
        chunks = iter(
            [
                {"type": "http.request", "body": b"12345", "more_body": True},
                {"type": "http.request", "body": b"67890", "more_body": False},
            ]
        )
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(chunks)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(middleware(self._scope(b"application/json"), receive, send))

        assert sent[0]["status"] == 413
        assert not reached, "oversized body must never reach the app"

    def test_small_json_body_is_replayed(self):
        reached: list[bytes] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            reached.append((await receive())["body"])

        middleware = JsonBodyLimitMiddleware(app, max_bytes=64)
        chunks = iter(
            [
                {"type": "http.request", "body": b'{"a":', "more_body": True},
                {"type": "http.request", "body": b"1}", "more_body": False},
            ]
        )
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(chunks)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(middleware(self._scope(b"application/json"), receive, send))

        assert reached == [b'{"a":1}']

    def test_multipart_is_not_capped_by_the_json_limit(self):
        """Uploads keep their own (much larger) per-endpoint limits."""
        reached: list[bool] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            reached.append(True)

        middleware = JsonBodyLimitMiddleware(app, max_bytes=4)

        async def receive() -> dict[str, Any]:  # pragma: no cover — must not run
            raise AssertionError("multipart must stream through untouched")

        async def send(message: dict[str, Any]) -> None:  # pragma: no cover
            raise AssertionError("no response")

        asyncio.run(middleware(self._scope(b"multipart/form-data; boundary=x"), receive, send))
        assert reached == [True]


# ── 3. WebSocket Origin gate ───────────────────────────────────────


class TestWebSocketOrigin:
    def test_cross_site_origin_is_rejected(self, client: TestClient):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()

    def test_local_origin_is_allowed(self, client: TestClient):
        with client.websocket_connect("/ws", headers={"origin": "http://localhost:5173"}) as ws:
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json()["type"] == "pong"


# ── 4. upload caps ─────────────────────────────────────────────────


class TestUploadCaps:
    def test_import_file_accepts_gbk_modlist(self, client: TestClient, monkeypatch):
        """A GBK/ANSI modlist used to raise UnicodeDecodeError → 500."""
        from rwmod.routers import download as dl_router

        async def fake_download(*_args: object, **_kwargs: object) -> bool:
            return True

        monkeypatch.setattr(dl_router, "_bounded_download", fake_download)
        payload = "2009463077\n# 中文注释\n".encode("gbk")

        resp = client.post("/api/import/file", files={"file": ("mods.txt", payload, "text/plain")})

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1

    def test_saves_analyze_enforces_cap_for_chunked_upload(self, client: TestClient, monkeypatch):
        from rwmod.routers import download as dl_router

        monkeypatch.setattr(dl_router, "MAX_UPLOAD_BYTES", 8)
        resp = client.post(
            "/api/saves/analyze",
            files={"file": ("save.rws", b"0123456789abcdef", "application/octet-stream")},
        )
        assert resp.status_code == 413

    def test_compare_file_enforces_cap_for_chunked_upload(self, client: TestClient, monkeypatch):
        from rwmod.routers import download as dl_router

        monkeypatch.setattr(dl_router, "MAX_UPLOAD_BYTES", 8)
        resp = client.post(
            "/api/rimsort/compare-file",
            files={"file": ("ModsConfig.xml", b"0123456789abcdef", "text/xml")},
        )
        assert resp.status_code == 413

    def test_import_local_rejects_oversized_zip(self, client: TestClient, monkeypatch, tmp_path):
        from rwmod.routers import mods as mods_router

        monkeypatch.setattr(mods_router, "_MAX_MOD_ZIP_BYTES", 32)
        payload = b"PK" + b"x" * 128
        resp = client.post(
            "/api/mods/import-local", files={"file": ("mod.zip", payload, "application/zip")}
        )
        assert resp.status_code == 413

    def test_import_local_imports_a_valid_zip(self, client: TestClient, cfg: Config, tmp_path):
        zip_path = tmp_path / "mod.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "CoolMod/About/About.xml",
                "<ModMetaData><name>Cool</name><packageId>cool.mod</packageId></ModMetaData>",
            )

        resp = client.post(
            "/api/mods/import-local",
            files={"file": ("mod.zip", zip_path.read_bytes(), "application/zip")},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 1
        assert (cfg.mods_dir / "CoolMod" / "About" / "About.xml").is_file()


class TestWorkshopFailureContract:
    def test_search_network_failure_is_502_not_empty_200(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """``search_workshop`` returns ``None`` when Steam is unreachable.

        ``[]`` means "Steam answered, no matches"; the router must not render
        "could not ask" as an empty 200.
        """
        from rwmod.routers import workshop as ws_router

        def failing_search(_query: str, page: int = 1) -> None:
            return None

        monkeypatch.setattr(ws_router, "search_workshop", failing_search)

        resp = client.get("/api/search?q=harmony")

        assert resp.status_code == 502
        assert resp.json()["error"] == "WorkshopError"

    def test_search_empty_answer_is_a_200_empty_list(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Steam answering "no matches" is a successful empty result."""
        from rwmod.routers import workshop as ws_router

        monkeypatch.setattr(ws_router, "search_workshop", lambda _q, page=1: [])

        resp = client.get("/api/search?q=harmony")

        assert resp.status_code == 200
        assert resp.json() == {"results": []}

    def test_workshop_detail_network_failure_is_502_not_not_found(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod.routers import workshop as ws_router

        monkeypatch.setattr(ws_router, "fetch_item_details", lambda _ids: None)

        resp = client.get("/api/workshop/123")

        assert resp.status_code == 502
        assert resp.json()["error"] == "WorkshopError"

    def test_workshop_detail_empty_answer_is_found_false(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod.routers import workshop as ws_router

        monkeypatch.setattr(ws_router, "fetch_item_details", lambda _ids: {})

        resp = client.get("/api/workshop/123")

        assert resp.status_code == 200
        assert resp.json() == {"found": False}

    def test_collection_preview_network_failure_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """An unreachable Steam is a 502 — never "the collection is empty"."""
        from rwmod.routers import workshop as ws_router

        monkeypatch.setattr(ws_router, "fetch_collection_children", lambda _cid: None)

        resp = client.get("/api/collection/preview/123456")

        assert resp.status_code == 502
        assert resp.json()["error"] == "WorkshopError"

    def test_collection_preview_confirmed_empty_is_404(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod.routers import workshop as ws_router

        monkeypatch.setattr(ws_router, "fetch_collection_children", lambda _cid: [])

        resp = client.get("/api/collection/preview/123456")

        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"

    def test_collection_import_network_failure_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """The collection import endpoint must not 404 on a lookup it never made."""
        from rwmod.routers import download as dl_router

        monkeypatch.setattr(dl_router, "fetch_collection_children", lambda _cid: None)

        resp = client.post("/api/import/collection", json={"collection_id": "123456"})

        assert resp.status_code == 502
        assert resp.json()["error"] == "WorkshopError"


# ── 5. config robustness ───────────────────────────────────────────


class TestConfigRobustness:
    @staticmethod
    def _reload(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
        monkeypatch.setattr(Config, "CONFIG_PATH", path)
        Config._cache = None
        Config._cache_ts = 0.0
        Config._cache_path_key = None
        return Config.load()

    def test_utf8_bom_toml_is_accepted(self, tmp_path: Path, monkeypatch):
        path = tmp_path / ".rwmod.toml"
        path.write_bytes(b"\xef\xbb\xbf" + b'mods_dir = "D:/BomMods"\n')

        assert self._reload(monkeypatch, path).mods_dir == Path("D:/BomMods")

    def test_invalid_toml_falls_back_instead_of_500(self, tmp_path: Path, monkeypatch):
        path = tmp_path / ".rwmod.toml"
        path.write_text("this is [ not toml", encoding="utf-8")

        cfg = self._reload(monkeypatch, path)

        assert cfg.active_instance == "default"

    def test_current_instance_id_is_explicit_default_without_a_file(
        self, tmp_path: Path, monkeypatch
    ):
        self._reload(monkeypatch, tmp_path / "missing.toml")
        assert current_instance_id() == "default"


# ── 6. instance isolation ──────────────────────────────────────────


class TestInstanceIsolation:
    def test_resolve_workshop_ids_is_case_insensitive(self, tmp_path: Path):
        from rwmod.parser import resolve_workshop_ids

        _make_mod(tmp_path / "Mods", "Camel", "Author.CamelCase", workshop_id="123")

        known, unknown = resolve_workshop_ids(["author.camelcase"], tmp_path / "Mods")

        assert known == ["123"]
        assert unknown == []

    def test_mod_tag_query_is_instance_scoped(self, client: TestClient, cfg: Config):
        from rwmod.tags import add_tag

        _make_mod(cfg.mods_dir, "Tagged", "tagged.mod")
        add_tag("Tagged", "mine", instance_id="default")
        add_tag("Tagged", "theirs", instance_id="alpha")

        rows = client.get("/api/mods").json()

        tagged = next(r for r in rows if r["folder"] == "Tagged")
        assert tagged["tags"] == ["mine"]

    def test_cache_db_keeps_metadata_when_mods_dir_disappears(self, tmp_path: Path):
        from rwmod.cache_db import get_or_refresh_metas
        from rwmod.database import get_conn, init_db

        init_db()
        mods_dir = tmp_path / "Mods"
        _make_mod(mods_dir, "Kept", "kept.mod")
        assert len(get_or_refresh_metas(mods_dir, instance_id="alpha")) == 1

        # Same instance, temporarily unavailable directory (drive unplugged).
        assert get_or_refresh_metas(tmp_path / "Missing", instance_id="alpha") == []
        rows = (
            get_conn()
            .execute("SELECT folder FROM local_mod_metadata WHERE instance_id = 'alpha'")
            .fetchall()
        )
        assert [r["folder"] for r in rows] == ["Kept"]

    def test_queue_cancel_is_instance_scoped(self):
        q = DownloadQueue()
        alpha = QueueItem(id="111", instance_id="alpha", status="downloading")
        beta = QueueItem(id="111", instance_id="beta", status="downloading")
        q.items = [alpha, beta]

        assert q.remove("111", instance_id="alpha")

        assert alpha.status == "cancelled"
        assert beta.status == "downloading"
        assert ("alpha", "111") in q._cancelled
        assert ("beta", "111") not in q._cancelled

    def test_remove_requests_cancel_before_the_process_spawns(self, monkeypatch):
        """The batch may still be probing / waiting for the global lock."""
        import rwmod.steamcmd as steamcmd_mod

        requests: list[tuple[str | None, str]] = []
        monkeypatch.setattr(
            steamcmd_mod, "request_cancel", lambda iid, mid: requests.append((iid, mid))
        )

        q = DownloadQueue()
        q.items.append(QueueItem(id="123", status="downloading"))

        assert q.remove("123")
        assert requests == [("default", "123")]

    def test_queue_clear_done_is_instance_scoped(self):
        from rwmod.database import init_db, queue_load_all, queue_upsert

        init_db()
        queue_upsert("111", instance_id="alpha", status="done")
        queue_upsert("111", instance_id="beta", status="pending")

        q = DownloadQueue()
        q.items = [
            QueueItem(id="111", instance_id="alpha", status="done"),
            QueueItem(id="111", instance_id="beta", status="pending"),
        ]
        q.clear_done()

        assert q.snapshot() == [
            {
                "id": "111",
                "name": "",
                "instance_id": "beta",
                "status": "pending",
                "progress": 0.0,
                "msg": "",
                "downloaded": 0.0,
                "total": 0.0,
                "speed_bps": 0.0,
                "error_kind": "",
                "error_detail": "",
            }
        ]
        rows = {(r["instance_id"], r["workshop_id"], r["status"]) for r in queue_load_all()}
        assert rows == {("beta", "111", "pending")}


# ── 7. atomic writes ───────────────────────────────────────────────


class TestAtomicWrites:
    @staticmethod
    def _live(path: Path) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<ModsConfigData><version>1.5</version>"
            "<activeMods><li>a.mod</li></activeMods></ModsConfigData>\n"
        )

    def test_undo_replaces_atomically_and_leaves_no_tmp(self, tmp_path: Path, monkeypatch):
        from rwmod.undo import snapshot_modsconfig, undo_last

        monkeypatch.setattr("rwmod.undo.UNDO_DIR", tmp_path / "undo")
        live = tmp_path / "ModsConfig.xml"
        live.write_text(self._live(live), encoding="utf-8")
        snapshot_modsconfig(live, label="test", instance_id="default")

        live.write_text("<truncated", encoding="utf-8")
        undo_last(live, instance_id="default")

        assert "<activeMods>" in live.read_text(encoding="utf-8")
        assert not list(tmp_path.glob("*.rwmod.tmp"))

    def test_undo_failure_never_truncates_the_live_file(self, tmp_path: Path, monkeypatch):
        from rwmod.undo import snapshot_modsconfig, undo_last

        monkeypatch.setattr("rwmod.undo.UNDO_DIR", tmp_path / "undo")
        live = tmp_path / "ModsConfig.xml"
        original = self._live(live)
        live.write_text(original, encoding="utf-8")
        snapshot_modsconfig(live, label="test", instance_id="default")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("rwmod.undo.os.replace", boom)
        result = undo_last(live, instance_id="default")

        assert result["ok"] is False
        assert live.read_text(encoding="utf-8") == original
        assert not list(tmp_path.glob("*.rwmod.tmp"))

    def test_restore_profile_never_removes_the_live_file_first(self, tmp_path: Path, monkeypatch):
        from rwmod.profile import restore_profile, save_profile

        monkeypatch.setattr("rwmod.profile.PROFILES_DIR", tmp_path / "profiles")
        target = tmp_path / "ModsConfig.xml"
        original = self._live(target)
        target.write_text(original, encoding="utf-8")
        save_profile(
            "p",
            "<ModsConfigData><activeMods><li>other.mod</li></activeMods></ModsConfigData>",
            instance_id="default",
        )

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("rwmod.profile.os.replace", boom)
        result = restore_profile("p", target, instance_id="default")

        assert result["ok"] is False
        # The live file is still there, byte-for-byte; .bak holds a copy.
        assert target.read_text(encoding="utf-8") == original
        assert target.with_suffix(".xml.rwmod.bak").exists()
        assert not target.with_suffix(".xml.rwmod.tmp").exists()


# ── 8. mod-settings backup partitioning ────────────────────────────


class TestModSettingsBackups:
    def test_backups_are_partitioned_per_instance(self, tmp_path: Path, monkeypatch):
        from rwmod.mod_settings import backups_dir

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        default_dir = backups_dir()
        alpha_dir = backups_dir("alpha")

        assert default_dir != alpha_dir
        assert default_dir.name == "default"
        assert alpha_dir.name == "alpha"
        assert default_dir.parent == alpha_dir.parent


# ── 9. rimsort apply: format preservation + CSRF guard ─────────────


class TestRimsortApply:
    def _config_dir(self, cfg: Config, tmp_path: Path) -> Path:
        config_dir = tmp_path / "explicit_config"
        config_dir.mkdir()
        cfg.config_dir = config_dir
        cfg.save()
        return config_dir

    def test_apply_preserves_bom_comments_and_crlf(self, cfg: Config, tmp_path: Path):
        from rwmod.routers.rimsort import rimsort_apply

        config_dir = self._config_dir(cfg, tmp_path)
        target = config_dir / "ModsConfig.xml"
        target.write_bytes(
            b"\xef\xbb\xbf"
            b'<?xml version="1.0" encoding="utf-8"?>\r\n'
            b"<ModsConfigData>\r\n"
            b"  <!-- keep this comment -->\r\n"
            b"  <version>1.5</version>\r\n"
            b"  <activeMods>\r\n"
            b"    <li>b.mod</li>\r\n"
            b"    <li>a.mod</li>\r\n"
            b"  </activeMods>\r\n"
            b"</ModsConfigData>\r\n"
        )
        _make_mod(cfg.mods_dir, "A", "a.mod")
        _make_mod(cfg.mods_dir, "B", "b.mod")

        rimsort_apply(cfg=cfg)

        raw = target.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"<!-- keep this comment -->" in raw
        assert b"\r\n" in raw
        text = raw.decode("utf-8-sig")
        assert text.index("<li>a.mod</li>") < text.index("<li>b.mod</li>")
        assert not list(config_dir.glob("*.rwmod.tmp"))
        # A timestamped backup of the original survives.
        assert list(config_dir.glob("ModsConfig.xml.bak_*"))

    def test_apply_keeps_exactly_one_bom_and_never_adds_one(self, cfg: Config, tmp_path: Path):
        from rwmod.routers.rimsort import rimsort_apply

        config_dir = self._config_dir(cfg, tmp_path)
        target = config_dir / "ModsConfig.xml"
        target.write_bytes(
            b"\xef\xbb\xbf"
            b'<?xml version="1.0" encoding="utf-8"?>\r\n'
            b"<ModsConfigData>\r\n"
            b"  <activeMods>\r\n"
            b"    <li>b.mod</li>\r\n"
            b"    <li>a.mod</li>\r\n"
            b"  </activeMods>\r\n"
            b"</ModsConfigData>\r\n"
        )
        _make_mod(cfg.mods_dir, "A", "a.mod")
        _make_mod(cfg.mods_dir, "B", "b.mod")

        rimsort_apply(cfg=cfg)

        raw = target.read_bytes()
        assert raw.count(b"\xef\xbb\xbf") == 1
        assert raw.index(b"<li>a.mod</li>") < raw.index(b"<li>b.mod</li>")

        # A BOM-less file must stay BOM-less after a sort.
        target.write_bytes(
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b"<ModsConfigData>\n"
            b"  <activeMods>\n"
            b"    <li>b.mod</li>\n"
            b"  </activeMods>\n"
            b"</ModsConfigData>\n"
        )
        rimsort_apply(cfg=cfg)
        assert not target.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_refresh_rules_failure_is_a_502(
        self, client: TestClient, cfg: Config, monkeypatch: pytest.MonkeyPatch
    ):
        """An explicit refresh that failed must not be reported as success."""
        import rwmod.routers.rimsort as rimsort_router
        from rwmod.rimpy_db import RimPyDB

        monkeypatch.setattr(rimsort_router, "refresh_community_rules", lambda: False)
        monkeypatch.setattr(RimPyDB.get(), "last_refresh_error", "网络不可用")
        _make_mod(cfg.mods_dir, "A", "a.mod")

        resp = client.post("/api/rimsort/sort?refresh_rules=true", json={"active_ids": ["a.mod"]})

        assert resp.status_code == 502
        assert resp.json()["error"] == "DownloadError"
        assert "刷新失败" in resp.json()["detail"]

    def test_apply_rejects_cross_site_origin(self, client: TestClient):
        resp = client.post("/api/rimsort/apply", headers={"origin": "https://evil.example"})
        assert resp.status_code == 403
        assert resp.json()["error"] == "HTTPError"

    def test_apply_allows_same_origin(self, client: TestClient, cfg: Config, tmp_path: Path):
        config_dir = self._config_dir(cfg, tmp_path)
        (config_dir / "ModsConfig.xml").write_text(
            "<ModsConfigData><activeMods><li>a.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        _make_mod(cfg.mods_dir, "A", "a.mod")

        resp = client.post("/api/rimsort/apply", headers={"origin": "http://testserver"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


# ── 10. response models are documented ─────────────────────────────


class TestResponseModels:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/mods"),
            ("get", "/api/queue"),
            ("get", "/api/history"),
            ("get", "/api/history/stats"),
            ("get", "/api/backups"),
            ("get", "/api/profiles"),
            ("get", "/api/tags"),
            ("get", "/api/config"),
            ("get", "/api/rimsort/check-order"),
            ("get", "/api/rimsort/steamdb/status"),
            ("post", "/api/rimsort/sort"),
            ("post", "/api/rimsort/apply"),
            ("get", "/api/saves"),
            ("get", "/api/mod-settings"),
            ("get", "/api/dashboard"),
        ],
    )
    def test_200_response_is_typed(self, method: str, path: str):
        from rwmod.server import app

        operation = app.openapi()["paths"][path][method]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]

        assert schema, f"{method.upper()} {path} has no response schema"
        assert "$ref" in schema or "$ref" in str(schema), schema

    def test_queue_snapshot_matches_the_response_model(self):
        response = QueueStateResponse.model_validate(
            {"items": [item for item in DownloadQueue().snapshot()], "paused": False}
        )
        assert response.paused is False
