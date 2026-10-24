"""API integration tests — verify all endpoints return expected status codes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def cfg():
    """The config the isolated TestClient is serving (same isolated paths)."""
    from rwmod.config import Config

    return Config.load()


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
            "instances",
            "active_instance",
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


class TestInstanceEndpoints:
    """Wave 6a — multi-instance API: list, create, activate, delete."""

    def _create(self, client: TestClient, tmp_path: Path, instance_id: str = "second"):
        mods = tmp_path / f"{instance_id}_Mods"
        rimworld = tmp_path / f"{instance_id}_RimWorld"
        return client.post(
            "/api/config/instances",
            json={
                "id": instance_id,
                "name": instance_id.title(),
                "mods_dir": str(mods),
                "rimworld_dir": str(rimworld),
            },
        )

    def test_config_reports_instances_and_active(self, client: TestClient):
        body = client.get("/api/config").json()
        assert body["active_instance"] == "default"
        assert [i["id"] for i in body["instances"]] == ["default"]
        assert body["instances"][0]["mods_dir"] == body["mods_dir"]

    def test_create_activate_and_delete(self, client: TestClient, tmp_path: Path):
        resp = self._create(client, tmp_path)
        assert resp.status_code == 200
        instance = resp.json()["instance"]
        assert instance["id"] == "second"
        assert instance["config_dir"] is None

        assert self._create(client, tmp_path).status_code == 409  # duplicate id

        assert client.post("/api/config/instances/second/activate").status_code == 200
        body = client.get("/api/config").json()
        assert body["active_instance"] == "second"
        assert body["mods_dir"] == str(tmp_path / "second_Mods")

        # Cannot delete the active instance.
        assert client.delete("/api/config/instances/second").status_code == 400

        assert client.post("/api/config/instances/default/activate").status_code == 200
        assert client.delete("/api/config/instances/second").status_code == 200
        assert [i["id"] for i in client.get("/api/config").json()["instances"]] == ["default"]

    def test_cannot_delete_the_last_instance(self, client: TestClient):
        resp = client.delete("/api/config/instances/default")
        assert resp.status_code == 400

    def test_activate_unknown_instance_is_404(self, client: TestClient):
        assert client.post("/api/config/instances/ghost/activate").status_code == 404

    def test_create_rejects_relative_mods_dir(self, client: TestClient):
        resp = client.post(
            "/api/config/instances",
            json={"id": "bad", "mods_dir": "relative/path", "rimworld_dir": "D:/RimWorld"},
        )
        assert resp.status_code == 400

    def test_activate_is_blocked_by_pending_downloads(self, client: TestClient, tmp_path):
        assert self._create(client, tmp_path).status_code == 200
        from rwmod.database import queue_upsert

        queue_upsert("123456", status="pending")
        resp = client.post("/api/config/instances/second/activate")
        assert resp.status_code == 409
        assert client.get("/api/config").json()["active_instance"] == "default"

    def test_activation_clears_the_memory_mod_cache(self, client: TestClient, tmp_path):
        assert self._create(client, tmp_path).status_code == 200
        import rwmod.mod_cache as mod_cache

        mod_cache._cache["sentinel"] = ({}, 0.0, 0.0)
        try:
            assert client.post("/api/config/instances/second/activate").status_code == 200
            assert "sentinel" not in mod_cache._cache
        finally:
            mod_cache.clear_memory_cache()

    def test_switching_does_not_see_another_instances_tags(self, client: TestClient, tmp_path):
        """The switch itself is what makes scoped reads follow the new instance."""
        from rwmod.tags import add_tag, get_tags

        assert self._create(client, tmp_path).status_code == 200
        add_tag("Shared", "DefaultOnly")  # active instance = default
        assert client.post("/api/config/instances/second/activate").status_code == 200
        assert get_tags("Shared") == []
        add_tag("Shared", "SecondOnly")
        assert get_tags("Shared") == ["SecondOnly"]
        assert client.post("/api/config/instances/default/activate").status_code == 200
        assert get_tags("Shared") == ["DefaultOnly"]


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


class TestModsActiveEndpoint:
    """POST /api/mods/active and the `active` flag on GET /api/mods."""

    @pytest.fixture(autouse=True)
    def _sandbox_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every path this class writes must stay inside pytest's tmp tree."""
        monkeypatch.setenv("RWMOD_TEST_SANDBOX_HOME", str(tmp_path))

    @staticmethod
    def _make_mod(cfg, folder: str, package_id: str) -> None:
        about = cfg.mods_dir / folder / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            f"<ModMetaData><name>{folder}</name><packageId>{package_id}</packageId></ModMetaData>",
            encoding="utf-8",
        )

    @staticmethod
    def _write_config(cfg, active_ids: list[str]) -> Path:
        target = Path(cfg.rimworld_dir) / "ModsConfig.xml"
        items = "".join(f"<li>{pid}</li>" for pid in active_ids)
        target.write_text(
            f"<ModsConfigData><version>1.6</version>"
            f"<activeMods>{items}</activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        return target

    def test_list_mods_reports_active_case_insensitively(self, client: TestClient, cfg):
        """RimWorld lowercases ModsConfig ids while About.xml keeps the casing."""
        self._make_mod(cfg, "MixedCase", "Author.MixedCase")
        self._make_mod(cfg, "Inactive", "inactive.mod")
        self._write_config(cfg, ["author.mixedcase"])

        mods = client.get("/api/mods").json()
        by_folder = {m["folder"]: m for m in mods}

        assert by_folder["MixedCase"]["active"] is True
        assert by_folder["Inactive"]["active"] is False
        assert all("active" in m for m in mods)

    def test_list_mods_without_modsconfig_marks_everything_inactive(self, client: TestClient, cfg):
        self._make_mod(cfg, "Lonely", "lonely.mod")

        mods = client.get("/api/mods").json()
        assert [m["active"] for m in mods] == [False]

    def test_list_mods_with_malformed_modsconfig_degrades_to_inactive(
        self, client: TestClient, cfg
    ):
        """Listing is read-only: a broken config must not fail the whole page."""
        self._make_mod(cfg, "Lonely", "lonely.mod")
        (cfg.rimworld_dir / "ModsConfig.xml").write_text("<broken>", encoding="utf-8")

        resp = client.get("/api/mods")

        assert resp.status_code == 200
        assert [m["active"] for m in resp.json()] == [False]

    def test_enable_appends_and_returns_backup(self, client: TestClient, cfg, tmp_path):
        self._make_mod(cfg, "Fresh", "fresh.mod")
        target = self._write_config(cfg, ["brrainz.harmony"])
        assert Path(target).is_relative_to(tmp_path)

        resp = client.post("/api/mods/active", json={"enable": ["fresh.mod"]})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["added"] == ["fresh.mod"]
        assert body["removed"] == []
        assert body["skipped"] == []
        assert body["active_count"] == 2
        assert body["needs_sort"] is False
        assert body["backup"] and body["backup"].startswith("ModsConfig.xml.bak_")
        assert body["backup"] in [p.name for p in Path(target).parent.glob("*.bak_*")]
        written = Path(target).read_text(encoding="utf-8")
        assert written.index("brrainz.harmony") < written.index("fresh.mod")

    def test_disable_removes_only_the_requested_mod(self, client: TestClient, cfg):
        self._make_mod(cfg, "Fresh", "fresh.mod")
        target = self._write_config(cfg, ["brrainz.harmony", "fresh.mod", "keep.mod"])

        body = client.post("/api/mods/active", json={"disable": ["FRESH.MOD"]}).json()

        assert body["removed"] == ["fresh.mod"]
        assert body["added"] == []
        written = Path(target).read_text(encoding="utf-8")
        assert "fresh.mod" not in written
        assert "keep.mod" in written

    def test_unknown_ids_are_skipped_without_failing(self, client: TestClient, cfg):
        self._write_config(cfg, ["brrainz.harmony"])

        resp = client.post(
            "/api/mods/active", json={"enable": ["ghost.mod"], "disable": ["nope.mod"]}
        )

        assert resp.status_code == 200
        assert resp.json()["skipped"] == ["nope.mod", "ghost.mod"]

    def test_missing_modsconfig_is_404(self, client: TestClient):
        resp = client.post("/api/mods/active", json={"enable": ["a.b"]})
        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"

    def test_malformed_modsconfig_is_400(self, client: TestClient, cfg):
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.write_text("<ModsConfigData><activeMods>", encoding="utf-8")

        resp = client.post("/api/mods/active", json={"enable": ["a.b"]})
        assert resp.status_code == 400
        assert resp.json()["error"] == "ValidationError"

    def test_empty_request_is_400(self, client: TestClient, cfg):
        self._write_config(cfg, ["brrainz.harmony"])

        resp = client.post("/api/mods/active", json={})
        assert resp.status_code == 400

    def test_sort_true_anchors_official_content(self, client: TestClient, cfg):
        self._make_mod(cfg, "Community", "community.mod")
        target = self._write_config(cfg, ["community.mod", "ludeon.rimworld"])

        body = client.post("/api/mods/active", json={"sort": True}).json()

        assert body["needs_sort"] is False
        written = Path(target).read_text(encoding="utf-8")
        assert written.index("ludeon.rimworld") < written.index("community.mod")

    def test_sort_keeps_unresolved_active_ids(self, client: TestClient, cfg):
        """A stale id cannot be sorted but must never be dropped."""
        target = self._write_config(cfg, ["ghost.mod", "ludeon.rimworld"])

        resp = client.post("/api/mods/active", json={"sort": True})

        assert resp.status_code == 200, resp.text
        written = Path(target).read_text(encoding="utf-8")
        assert "ghost.mod" in written
        assert written.index("ludeon.rimworld") < written.index("ghost.mod")


class TestModSettingsEndpoint:
    """Config/Mod_*.xml listing, raw editing and backups over HTTP."""

    @pytest.fixture(autouse=True)
    def _sandbox_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every path this class writes must stay inside pytest's tmp tree."""
        monkeypatch.setenv("RWMOD_TEST_SANDBOX_HOME", str(tmp_path))

    @staticmethod
    def _use_config_dir(cfg, tmp_path: Path) -> Path:
        from rwmod.config import Config

        config_dir = tmp_path / "explicit_config"
        config_dir.mkdir()
        cfg.config_dir = config_dir
        cfg.save()
        assert Config.load().config_dir == config_dir
        return config_dir

    @staticmethod
    def _write_setting(config_dir: Path, name: str, xml: str) -> Path:
        target = config_dir / name
        target.write_text(xml, encoding="utf-8")
        assert target.is_relative_to(config_dir.parent)
        return target

    def test_list_without_config_dir_is_empty(self, client: TestClient):
        resp = client.get("/api/mod-settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["files"] == []
        assert body["config_dir"] is None

    def test_list_only_mod_settings_files(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        self._write_setting(config_dir, "Mod_1_Thing.xml", "<a/>")
        self._write_setting(config_dir, "KeyPrefs.xml", "<b/>")

        body = client.get("/api/mod-settings").json()

        assert body["config_dir"] == str(config_dir)
        assert [f["filename"] for f in body["files"]] == ["Mod_1_Thing.xml"]

    def test_get_returns_raw_xml(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        self._write_setting(config_dir, "Mod_1_Thing.xml", "<ModSettings><v>1</v></ModSettings>")

        resp = client.get("/api/mod-settings/Mod_1_Thing.xml")

        assert resp.status_code == 200
        body = resp.json()
        assert body["xml"] == "<ModSettings><v>1</v></ModSettings>"
        assert body["well_formed"] is True
        assert body["root_tag"] == "ModSettings"

    def test_get_missing_file_is_404(self, client: TestClient, cfg, tmp_path: Path):
        self._use_config_dir(cfg, tmp_path)

        resp = client.get("/api/mod-settings/Mod_9_Nope.xml")
        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"

    def test_non_mod_filename_is_400(self, client: TestClient, cfg, tmp_path: Path):
        self._use_config_dir(cfg, tmp_path)

        resp = client.get("/api/mod-settings/KeyPrefs.xml")
        assert resp.status_code == 400
        assert resp.json()["error"] == "ValidationError"

    def test_put_requires_ack(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        target = self._write_setting(config_dir, "Mod_1_Thing.xml", "<a/>")

        resp = client.put("/api/mod-settings/Mod_1_Thing.xml", json={"xml": "<b/>"})

        assert resp.status_code == 400
        assert target.read_text(encoding="utf-8") == "<a/>"

    def test_put_writes_and_backs_up(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        target = self._write_setting(config_dir, "Mod_1_Thing.xml", "<a/>")

        resp = client.put(
            "/api/mod-settings/Mod_1_Thing.xml",
            json={"xml": "<b/>", "ack_game_closed": True},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["backup"]
        assert target.read_text(encoding="utf-8") == "<b/>"
        backup = Path.home() / ".rwmod" / "mod_settings_backups" / body["backup"]
        assert backup.is_relative_to(tmp_path)
        assert backup.read_text(encoding="utf-8") == "<a/>"

    def test_put_malformed_xml_is_400(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        target = self._write_setting(config_dir, "Mod_1_Thing.xml", "<a/>")

        resp = client.put(
            "/api/mod-settings/Mod_1_Thing.xml",
            json={"xml": "<broken>", "ack_game_closed": True},
        )

        assert resp.status_code == 400
        assert target.read_text(encoding="utf-8") == "<a/>"

    def test_put_oversized_is_400(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        self._write_setting(config_dir, "Mod_1_Thing.xml", "<a/>")

        resp = client.put(
            "/api/mod-settings/Mod_1_Thing.xml",
            json={"xml": "<a>" + "x" * (2 * 1024 * 1024) + "</a>", "ack_game_closed": True},
        )

        assert resp.status_code == 400

    def test_restore_latest_backup(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        target = self._write_setting(config_dir, "Mod_1_Thing.xml", "<v1/>")
        client.put(
            "/api/mod-settings/Mod_1_Thing.xml",
            json={"xml": "<v2/>", "ack_game_closed": True},
        )
        assert target.read_text(encoding="utf-8") == "<v2/>"

        resp = client.post(
            "/api/mod-settings/Mod_1_Thing.xml/restore",
            json={"ack_game_closed": True},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["restored_from"]
        assert target.read_text(encoding="utf-8") == "<v1/>"

    def test_restore_without_backups_is_404(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        self._write_setting(config_dir, "Mod_9_Never.xml", "<a/>")

        resp = client.post(
            "/api/mod-settings/Mod_9_Never.xml/restore",
            json={"ack_game_closed": True},
        )

        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"

    def test_backup_all_then_list_backups(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        self._write_setting(config_dir, "Mod_1_Thing.xml", "<a/>")
        self._write_setting(config_dir, "Mod_2_Other.xml", "<b/>")

        backed_up = client.post("/api/mod-settings/backup-all")
        assert backed_up.status_code == 200
        assert backed_up.json()["count"] == 2

        listed = client.get("/api/mod-settings/backups")
        assert listed.status_code == 200
        body = listed.json()
        assert {b["source"] for b in body["backups"]} == {
            "Mod_1_Thing.xml",
            "Mod_2_Other.xml",
        }
        assert Path(body["backup_dir"]).is_relative_to(tmp_path)


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

    def test_save_detail_matches_package_ids_case_insensitively(self, client: TestClient, cfg):
        """RimWorld lowercases save ids; About.xml keeps the original casing."""
        about = cfg.mods_dir / "MixedCase" / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            "<ModMetaData><packageId>Author.MixedCase</packageId></ModMetaData>",
            encoding="utf-8",
        )
        saves_dir = cfg.rimworld_dir / "Saves"
        saves_dir.mkdir()
        (saves_dir / "Colony.rws").write_text(
            "<savegame><modIds><li>author.mixedcase</li></modIds></savegame>",
            encoding="utf-8",
        )

        detail = client.get("/api/saves/Colony")
        assert detail.status_code == 200
        body = detail.json()
        assert body["required_mods"] == ["author.mixedcase"]
        assert body["missing_mods"] == []
        assert body["loadable"] is True

        listing = client.get("/api/saves").json()["saves"]
        assert [s["name"] for s in listing] == ["Colony"]
        assert listing[0]["missing_count"] == 0

    def test_save_analysis_uses_the_shared_mod_cache(self, client: TestClient, cfg, monkeypatch):
        """The saves router must not re-parse every About.xml per request."""
        import rwmod.routers.saves as saves_router
        from rwmod.metadata import ModMeta

        calls: list = []

        def fake_get_cached_mods(mods_dir):
            calls.append(mods_dir)
            return [ModMeta(folder="Cached", package_id="cached.mod")]

        monkeypatch.setattr(saves_router, "get_cached_mods", fake_get_cached_mods)
        saves_dir = cfg.rimworld_dir / "Saves"
        saves_dir.mkdir()
        (saves_dir / "Cached.rws").write_text(
            "<savegame><modIds><li>cached.mod</li></modIds></savegame>",
            encoding="utf-8",
        )

        body = client.get("/api/saves/Cached").json()
        assert calls == [cfg.mods_dir]
        assert body["missing_mods"] == []
        assert body["loadable"] is True

    def test_download_missing_sees_no_missing_after_case_folding(self, client: TestClient, cfg):
        """A case-only difference must not trigger a download attempt."""
        about = cfg.mods_dir / "MixedCase" / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            "<ModMetaData><packageId>Author.MixedCase</packageId></ModMetaData>",
            encoding="utf-8",
        )
        saves_dir = cfg.rimworld_dir / "Saves"
        saves_dir.mkdir()
        (saves_dir / "Colony.rws").write_text(
            "<savegame><modIds><li>author.mixedcase</li></modIds></savegame>",
            encoding="utf-8",
        )

        resp = client.post("/api/saves/Colony/download-missing")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["results"] == []


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
