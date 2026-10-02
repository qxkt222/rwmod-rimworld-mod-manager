"""API integration tests — verify all endpoints return expected status codes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestHealthEndpoint:
    def test_status(self, client: TestClient):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "online" in data


class TestConfigEndpoint:
    def test_get_config(self, client: TestClient):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "steamcmd_path" in data
        assert "mods_dir" in data

    def test_config_exposes_exactly_the_documented_keys(self, client: TestClient):
        """ConfigResponse pins the shape in both directions: a key that goes
        missing breaks the panel, and a new one is a contract change."""
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert set(resp.json()) == {
            "steamcmd_path",
            "mods_dir",
            "rimworld_dir",
            "backup_dir",
            "has_steam_api_key",
            "steamcmd_exists",
            "mods_dir_exists",
        }

    def test_update_config(self, client: TestClient, tmp_path):
        resp = client.post("/api/config", json={"mods_dir": str(tmp_path / "OtherMods")})
        assert resp.status_code == 200
        assert resp.json()["ok"]

    def test_update_config_rejects_relative_path(self, client: TestClient):
        resp = client.post("/api/config", json={"mods_dir": "relative/path"})
        assert resp.status_code == 400

    def test_update_config_rejects_system_root(self, client: TestClient):
        # Refuse pointing mods/backup dirs at a filesystem root (C:\ on Windows).
        resp = client.post("/api/config", json={"mods_dir": "C:\\"})
        assert resp.status_code == 400


class TestModsEndpoint:
    def test_list_mods_empty(self, client: TestClient):
        resp = client.get("/api/mods")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_check_updates_empty(self, client: TestClient):
        resp = client.get("/api/mods/check-updates")
        assert resp.status_code == 200
        assert "updates" in resp.json()

    def test_export_mods_empty(self, client: TestClient):
        resp = client.get("/api/mods/export")
        assert resp.status_code == 200
        assert "mods" in resp.json()

    def test_export_collection_empty(self, client: TestClient):
        resp = client.get("/api/mods/export-collection")
        assert resp.status_code == 200
        assert "ids" in resp.json()

    def test_compatibility(self, client: TestClient):
        resp = client.get("/api/mods/compatibility")
        assert resp.status_code == 200
        data = resp.json()
        # A 200 body carries data only — "no RimWorld install" is a degraded
        # success, not an error. An `error` key here means the contract regressed.
        assert set(data) == {"rimworld_version", "groups"}, data

    def test_health_empty(self, client: TestClient):
        resp = client.get("/api/mods/health")
        assert resp.status_code == 200
        assert "mods" in resp.json()


class TestDashboardEndpoint:
    def test_dashboard(self, client: TestClient):
        resp = client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "mods_count" in data
        assert "updates_pending" in data
        assert "disk_usage_mb" in data


class TestDownloadEndpoint:
    def test_download_no_ids(self, client: TestClient):
        resp = client.post("/api/download", json={"ids": []})
        assert resp.status_code == 400

    def test_download_invalid_id(self, client: TestClient):
        resp = client.post("/api/download", json={"ids": ["not_a_number"]})
        assert resp.status_code == 400


class TestQueueEndpoint:
    def test_get_queue(self, client: TestClient):
        resp = client.get("/api/queue")
        assert resp.status_code == 200
        assert "items" in resp.json()

    def test_add_to_queue(self, client: TestClient):
        resp = client.post("/api/queue/add", json={"ids": ["123456"]})
        assert resp.status_code == 200
        assert resp.json()["added"] >= 0

    def test_clear_queue(self, client: TestClient):
        resp = client.post("/api/queue/clear")
        assert resp.status_code == 200
        assert resp.json()["ok"]


class TestAutoUpdateEndpoint:
    def test_status(self, client: TestClient):
        resp = client.get("/api/auto-update/status")
        assert resp.status_code == 200
        assert "running" in resp.json()

    def test_result(self, client: TestClient):
        resp = client.get("/api/auto-check/result")
        assert resp.status_code == 200
        assert "updates" in resp.json()


class TestHistoryEndpoint:
    def test_history(self, client: TestClient):
        resp = client.get("/api/history")
        assert resp.status_code == 200
        assert "items" in resp.json()

    def test_history_stats(self, client: TestClient):
        resp = client.get("/api/history/stats")
        assert resp.status_code == 200

    def test_clear_history(self, client: TestClient):
        resp = client.post("/api/history/clear")
        assert resp.status_code == 200
        assert resp.json()["ok"]


class TestRimsortEndpoint:
    def test_generate(self, client: TestClient):
        resp = client.post("/api/rimsort/generate")
        assert resp.status_code == 200
        assert "modsconfig_xml" in resp.json()

    def test_check_order_without_modsconfig(self, client: TestClient):
        """An isolated home has no ModsConfig.xml, so this is a 404 — not a 200
        body carrying error text."""
        resp = client.get("/api/rimsort/check-order")
        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"


class TestBackupsEndpoint:
    def test_list_backups(self, client: TestClient):
        resp = client.get("/api/backups")
        assert resp.status_code == 200
        assert "backups" in resp.json()


class TestProfilesEndpoint:
    def test_list_profiles(self, client: TestClient):
        resp = client.get("/api/profiles")
        assert resp.status_code == 200
        assert "profiles" in resp.json()

    def test_save_profile_no_name(self, client: TestClient):
        resp = client.post("/api/profiles/save", json={"name": ""})
        assert resp.status_code == 400


class TestSavesEndpoint:
    def test_missing_save_returns_404(self, client: TestClient):
        """A missing save fails via HTTP status, not a 200 body carrying `error`."""
        resp = client.get("/api/saves/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"
        assert resp.json()["detail"]


class TestWorkshopEndpoint:
    def test_search_no_query(self, client: TestClient):
        resp = client.get("/api/search?q=")
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    @pytest.mark.network
    def test_search(self, client: TestClient):
        """Hits the live Steam Workshop API — excluded by `-m "not network"`."""
        resp = client.get("/api/search?q=harmony")
        assert resp.status_code == 200
        assert "results" in resp.json()

    def test_search_result_shape_is_pinned(self, client: TestClient):
        """Search hits must expose exactly the modelled fields.

        They used to be ModSearchResult.__dict__, so any field added to that
        dataclass for internal reasons would have leaked into the API silently.
        """
        from unittest.mock import patch

        from rwmod.workshop import ModSearchResult

        hit = ModSearchResult(id="1", title="T", author="A")
        with patch("rwmod.routers.workshop.search_workshop", return_value=[hit]):
            resp = client.get("/api/search?q=harmony")

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert set(results[0]) == {
            "id",
            "title",
            "author",
            "description",
            "preview_url",
            "rating",
            "subscribers",
            "installed",
        }

    def test_collection_preview_missing_returns_404(self, client: TestClient):
        """An unfetchable collection fails via HTTP status, not a 200 body."""
        from unittest.mock import patch

        from rwmod.routers import workshop as ws_router

        with patch.object(ws_router, "fetch_collection_children", return_value=[]):
            resp = client.get("/api/collection/preview/123456")
        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"


class TestErrorHandling:
    def test_404_route(self, client: TestClient):
        resp = client.get("/api/nonexistent")
        assert resp.status_code == 404

    def test_error_response_format(self, client: TestClient):
        """Bad input answers with the one failure shape, never a bare `detail`.

        This endpoint raises HTTPException, so `error` is the generic marker that
        the site has not migrated to errors.py yet — the shape is what matters.
        """
        resp = client.post("/api/download", json={"ids": []})
        assert resp.status_code == 400
        assert resp.json()["error"] == "HTTPError"
        assert isinstance(resp.json()["detail"], str)
        assert resp.json()["detail"]

    def test_body_validation_uses_the_standard_shape(self, client: TestClient):
        """A non-object body is FastAPI's own validation path; its default body is
        {"detail": [...]}, a shape nothing else uses. It must be normalised."""
        resp = client.post("/api/queue/add", json=[1, 2, 3])
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "ValidationError"
        assert isinstance(body["detail"], str)


class TestDownloadAPIWithPatchedDownloader:
    """Download/import endpoints run SteamCMD in a thread — patched here."""

    def test_download_success(self, client: TestClient):
        from unittest.mock import patch

        from rwmod.routers import download as dl_router

        with patch.object(dl_router, "download_one", return_value=True):
            resp = client.post("/api/download", json={"ids": ["123456"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0] == {"id": "123456", "ok": True}

    def test_download_failure_recorded(self, client: TestClient):
        from unittest.mock import patch

        from rwmod.routers import download as dl_router

        with patch.object(dl_router, "download_one", return_value=False):
            resp = client.post("/api/download", json={"ids": ["999"]})
        assert resp.status_code == 200
        assert resp.json()["results"][0]["ok"] is False

    def test_import_file(self, client: TestClient):
        from unittest.mock import patch

        from rwmod.routers import download as dl_router

        with patch.object(dl_router, "download_one", return_value=True):
            resp = client.post(
                "/api/import/file",
                files={"file": ("mods.txt", b"123456\n# comment\n654321\n", "text/plain")},
            )
        assert resp.status_code == 200
        assert resp.json()["total"] == 2


class TestConfigApiKeyMasking:
    def test_config_does_not_leak_api_key(self, client: TestClient):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "steam_api_key" not in data
        assert "has_steam_api_key" in data


class TestOpenAPIContract:
    def test_documented_422_matches_what_the_handler_sends(self):
        """FastAPI documents its default HTTPValidationError body for 422 while
        validation_error_handler answers {error, detail}. /api/docs must not
        describe a body the server never sends."""
        from rwmod.server import app

        spec = app.openapi()
        assert set(spec["components"]["schemas"]["ErrorResponse"]["properties"]) == {
            "error",
            "detail",
        }

        documented = {
            op["responses"]["422"]["content"]["application/json"]["schema"].get("$ref")
            for operations in spec["paths"].values()
            for op in operations.values()
            if isinstance(op, dict) and "content" in op.get("responses", {}).get("422", {})
        }
        assert documented == {"#/components/schemas/ErrorResponse"}


class TestBackupTraversalAPI:
    def test_delete_backup_rejects_traversal(self, client: TestClient):
        resp = client.delete("/api/backups/..%5Cvictim.zip")
        # Starlette may 404 on some encoded separators — either way the file
        # must never be deleted; when routed, backup.delete_backup returns False.
        if resp.status_code == 200:
            assert resp.json()["ok"] is False
        else:
            assert resp.status_code in (404, 422)
