"""HTTP-layer contract probes (agent API-*) — endpoint inventory, failure contract,
auth posture, SSE/WebSocket framing, security headers and malformed-input tolerance.

Complements tests/test_api.py (which pins a handful of happy paths) by walking the
whole route surface programmatically and asserting the CLAUDE.md failure contract:

* failures answer ``{error, detail}`` with a *string* detail — never FastAPI's
  default ``{"detail": [...]}``;
* a 200 body carries data only (no ``error`` key);
* probe endpoints may answer ``{ok: false, msg}`` (``/api/steamcmd/check``).

Everything here is offline: Steam/Web-API calls and SteamCMD executions are
patched at the router boundary.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ── helpers ────────────────────────────────────────────────────────


def _assert_error(resp: Any, status: int) -> dict:
    """The one failure shape: {error, detail} with a string detail."""
    assert resp.status_code == status, f"{resp.status_code} != {status}: {resp.text[:300]}"
    body = resp.json()
    assert set(body) == {"error", "detail"}, f"unexpected failure body keys: {body}"
    assert isinstance(body["error"], str) and body["error"], body
    assert isinstance(body["detail"], str), (
        f"detail must be a string, got {type(body['detail']).__name__}: {body!r}"
    )
    assert body["detail"], body
    return body


def _assert_200_data(resp: Any) -> Any:
    """A 200 body carries data only — an `error` key is a contract regression."""
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    if isinstance(body, dict):
        assert "error" not in body, f"200 body carries an error key: {body}"
    return body


def _make_mod(
    mods_dir: Path,
    folder: str,
    package_id: str = "",
    name: str = "",
    workshop_id: str = "",
    chinese: bool = False,
) -> Path:
    """Create a minimal but valid mod folder under mods_dir."""
    d = mods_dir / folder
    about = d / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{name or folder}</name>"
        f"<packageId>{package_id or folder.lower()}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    if workshop_id:
        (about / "PublishedFileId.txt").write_text(workshop_id, encoding="utf-8")
    if chinese:
        (d / "Languages" / "ChineseSimplified" / "Keyed").mkdir(parents=True, exist_ok=True)
    return d


def _zip_with_mod(folder: str = "ZipMod", include_about: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if include_about:
            zf.writestr(
                f"{folder}/About/About.xml",
                f"<ModMetaData><name>{folder}</name><packageId>zip.{folder.lower()}</packageId>"
                "</ModMetaData>",
            )
        else:
            zf.writestr(f"{folder}/Readme.txt", "no about here")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _isolate_state_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect module-level state dirs bound at import time.

    ``undo.UNDO_DIR`` / ``profile.PROFILES_DIR`` / ``transfer.PROFILES_DIR`` are
    computed from ``Path.home()`` when their module is imported, so conftest's
    ``Path.home`` patch cannot reach them. Without this, an endpoint exercised
    here (rimsort/apply, profiles/save, undo) would write into the developer's
    real ``~/.rwmod``.
    """
    import rwmod.profile as profile_mod
    import rwmod.transfer as transfer_mod
    import rwmod.undo as undo_mod

    monkeypatch.setattr(undo_mod, "UNDO_DIR", tmp_path / "state_undo")
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", tmp_path / "state_profiles")
    monkeypatch.setattr(transfer_mod, "PROFILES_DIR", tmp_path / "state_profiles")


@pytest.fixture
def cfg(client: TestClient):  # noqa: ARG001 — depends on the isolated client
    from rwmod.config import Config

    return Config.load()


def _async_noop_result(payload: dict):
    async def _run(_self):
        return payload

    return _run


async def _async_method_noop(*_args: Any, **_kwargs: Any) -> None:
    """Class-attribute stub for an async method (survives binding)."""
    return None


def _request(client: TestClient, case: dict) -> Any:
    """Issue one table-driven request, applying the case's mock patches."""
    with patch.dict("os.environ", case.get("env", {}), clear=False):
        stack = [patch(target, value) for target, value in case.get("patches", {}).items()]
        for p in stack:
            p.start()
        try:
            return client.request(case["method"], case["url"], **case.get("kwargs", {}))
        finally:
            for p in reversed(stack):
                p.stop()


# ── request tables ─────────────────────────────────────────────────
# Each case: label, method, OpenAPI path template, concrete URL, kwargs, patches.
# `template` drives the route-coverage assertion; `url` is what actually runs.

FAILURE_CASES: list[dict] = [
    {
        "label": "unknown api path",
        "method": "GET",
        "template": "/api/nonexistent",
        "url": "/api/nonexistent",
        "status": 404,
    },
    {
        "label": "unknown non-api path",
        "method": "GET",
        "template": "/nope",
        "url": "/nope",
        "status": 404,
    },
    {
        "label": "wrong method",
        "method": "GET",
        "template": "/api/queue/clear",
        "url": "/api/queue/clear",
        "status": 405,
    },
    {
        "label": "malformed json",
        "method": "POST",
        "template": "/api/queue/add",
        "url": "/api/queue/add",
        "kwargs": {"content": b"{not json"},
        "status": 422,
    },
    {
        "label": "body not an object",
        "method": "POST",
        "template": "/api/queue/add",
        "url": "/api/queue/add",
        "kwargs": {"json": [1, 2, 3]},
        "status": 422,
    },
    {
        "label": "ids as string",
        "method": "POST",
        "template": "/api/download",
        "url": "/api/download",
        "kwargs": {"json": {"ids": "123"}},
        "status": 422,
    },
    {
        "label": "id item not a string",
        "method": "POST",
        "template": "/api/queue/add",
        "url": "/api/queue/add",
        "kwargs": {"json": {"ids": [1, 2]}},
        "status": 422,
    },
    {
        "label": "mods_dir wrong type",
        "method": "POST",
        "template": "/api/config",
        "url": "/api/config",
        "kwargs": {"json": {"mods_dir": 123}},
        "status": 422,
    },
    {
        "label": "query int invalid",
        "method": "GET",
        "template": "/api/history",
        "url": "/api/history?limit=abc",
        "status": 422,
    },
    {
        "label": "missing required upload",
        "method": "POST",
        "template": "/api/saves/analyze",
        "url": "/api/saves/analyze",
        "status": 422,
    },
    {
        "label": "backups cleanup keep=str",
        "method": "POST",
        "template": "/api/backups/cleanup",
        "url": "/api/backups/cleanup",
        "kwargs": {"json": {"keep": "x"}},
        "status": 422,
    },
    {
        "label": "download no valid ids",
        "method": "POST",
        "template": "/api/download",
        "url": "/api/download",
        "kwargs": {"json": {"ids": ["abc"]}},
        "status": 400,
    },
    {
        "label": "collection id invalid",
        "method": "POST",
        "template": "/api/import/collection",
        "url": "/api/import/collection",
        "kwargs": {"json": {"collection_id": "abc"}},
        "status": 400,
    },
    {
        "label": "profile empty name",
        "method": "POST",
        "template": "/api/profiles/save",
        "url": "/api/profiles/save",
        "kwargs": {"json": {"name": "  "}},
        "status": 400,
    },
    {
        "label": "rimsort compare empty xml",
        "method": "POST",
        "template": "/api/rimsort/compare",
        "url": "/api/rimsort/compare",
        "kwargs": {"json": {"xml": "  "}},
        "status": 400,
    },
    {
        "label": "tag empty",
        "method": "POST",
        "template": "/api/tags/{mod_folder}",
        "url": "/api/tags/f1",
        "kwargs": {"json": {"tag": "  "}},
        "status": 400,
    },
    # REV-006: mutations that changed nothing used to answer 200 {"ok": false}.
    {
        "label": "tag remove missing",
        "method": "DELETE",
        "template": "/api/tags/{mod_folder}/{tag}",
        "url": "/api/tags/folder1/t1",
        "status": 404,
    },
    {
        "label": "queue remove missing",
        "method": "DELETE",
        "template": "/api/queue/{mod_id}",
        "url": "/api/queue/123456",
        "status": 404,
    },
    {
        "label": "config relative path",
        "method": "POST",
        "template": "/api/config",
        "url": "/api/config",
        "kwargs": {"json": {"mods_dir": "rel/path"}},
        "status": 400,
    },
    {
        "label": "config filesystem root",
        "method": "POST",
        "template": "/api/config",
        "url": "/api/config",
        "kwargs": {"json": {"mods_dir": "C:\\"}},
        "status": 400,
    },
    {
        "label": "mods check-order without modsconfig",
        "method": "GET",
        "template": "/api/rimsort/check-order",
        "url": "/api/rimsort/check-order",
        "status": 404,
    },
    {
        "label": "save not found",
        "method": "GET",
        "template": "/api/saves/{save_name}",
        "url": "/api/saves/does-not-exist",
        "status": 404,
    },
    {
        "label": "download-missing save not found",
        "method": "POST",
        "template": "/api/saves/{save_name}/download-missing",
        "url": "/api/saves/does-not-exist/download-missing",
        "status": 404,
    },
    {
        "label": "upload oversized modlist",
        "method": "POST",
        "template": "/api/import/file",
        "url": "/api/import/file",
        "kwargs": {"files": {"file": ("m.txt", b"1" * (5 * 1024 * 1024 + 64), "text/plain")}},
        "status": 413,
    },
    {
        "label": "upload oversized compare-file",
        "method": "POST",
        "template": "/api/rimsort/compare-file",
        "url": "/api/rimsort/compare-file",
        "kwargs": {"files": {"file": ("m.xml", b"1" * (5 * 1024 * 1024 + 64), "text/xml")}},
        "status": 413,
    },
    {
        "label": "import-local not a zip",
        "method": "POST",
        "template": "/api/mods/import-local",
        "url": "/api/mods/import-local",
        "kwargs": {"files": {"file": ("mod.txt", b"hi", "text/plain")}},
        "status": 400,
    },
    {
        "label": "import-local empty zip",
        "method": "POST",
        "template": "/api/mods/import-local",
        "url": "/api/mods/import-local",
        "kwargs": {"files": {"file": ("mod.zip", b"", "application/zip")}},
        "status": 400,
    },
    {
        "label": "transfer import wrong ext",
        "method": "POST",
        "template": "/api/transfer/import",
        "url": "/api/transfer/import",
        "kwargs": {"files": {"file": ("b.txt", b"xx", "text/plain")}},
        "status": 400,
    },
    {
        "label": "transfer import not a bundle",
        "method": "POST",
        "template": "/api/transfer/import",
        "url": "/api/transfer/import",
        "kwargs": {"files": {"file": ("b.rwmod", b"not a zip", "application/zip")}},
        "status": 400,
    },
    {
        "label": "batch download no valid ids",
        "method": "POST",
        "template": "/api/mods/batch/download",
        "url": "/api/mods/batch/download",
        "kwargs": {"json": {"ids": ["nope"]}},
        "status": 400,
    },
    {
        "label": "batch delete no folders",
        "method": "POST",
        "template": "/api/mods/batch/delete",
        "url": "/api/mods/batch/delete",
        "kwargs": {"json": {"folders": []}},
        "status": 400,
    },
    {
        "label": "mods active without modsconfig",
        "method": "POST",
        "template": "/api/mods/active",
        "url": "/api/mods/active",
        "kwargs": {"json": {"enable": ["a.b"]}},
        "status": 404,
    },
    {
        "label": "mod settings get missing",
        "method": "GET",
        "template": "/api/mod-settings/{filename}",
        "url": "/api/mod-settings/Mod_1_Nope.xml",
        "status": 404,
    },
    {
        "label": "mod settings restore without ack",
        "method": "POST",
        "template": "/api/mod-settings/{filename}/restore",
        "url": "/api/mod-settings/Mod_1_Nope.xml/restore",
        "kwargs": {"json": {}},
        "status": 400,
    },
    {
        "label": "download stream bad id",
        "method": "GET",
        "template": "/api/download/stream",
        "url": "/api/download/stream?id=abc",
        "status": 400,
    },
    {
        "label": "collection preview not found",
        "method": "GET",
        "template": "/api/collection/preview/{collection_id}",
        "url": "/api/collection/preview/123456",
        "patches": {"rwmod.routers.workshop.fetch_collection_children": lambda _cid: []},
        "status": 404,
    },
    {
        "label": "collection preview fetch error",
        "method": "GET",
        "template": "/api/collection/preview/{collection_id}",
        "url": "/api/collection/preview/123456",
        "patches": {
            "rwmod.routers.workshop.fetch_collection_children": lambda _cid: (_ for _ in ()).throw(
                RuntimeError("network down")
            )
        },
        "status": 502,
    },
    {
        "label": "activate unknown instance",
        "method": "POST",
        "template": "/api/config/instances/{instance_id}/activate",
        "url": "/api/config/instances/ghost/activate",
        "status": 404,
    },
    {
        "label": "delete unknown instance",
        "method": "DELETE",
        "template": "/api/config/instances/{instance_id}",
        "url": "/api/config/instances/ghost",
        "status": 404,
    },
    {
        "label": "import collection unfetchable",
        "method": "POST",
        "template": "/api/import/collection",
        "url": "/api/import/collection",
        "kwargs": {"json": {"collection_id": "123456"}},
        "patches": {"rwmod.routers.download.fetch_collection_children": lambda _cid: []},
        "status": 404,
    },
    # ── failure-contract migration (review 0.7.0, BE-A item 6) ──────
    # These five used to answer 200 with `{ok: false, msg}`. A missing
    # backup/profile/snapshot is a failure — the status code must carry it,
    # exactly like every other error in this table.
    {
        "label": "backup delete missing",
        "method": "DELETE",
        "template": "/api/backups/{filename}",
        "url": "/api/backups/nope.zip",
        "status": 404,
    },
    {
        "label": "backup restore missing",
        "method": "POST",
        "template": "/api/backups/{workshop_id}/restore",
        "url": "/api/backups/123/restore",
        "kwargs": {"json": {}},
        "status": 404,
    },
    {
        "label": "profiles restore missing",
        "method": "POST",
        "template": "/api/profiles/{name}/restore",
        "url": "/api/profiles/nope/restore",
        "status": 404,
    },
    {
        "label": "profiles delete missing",
        "method": "DELETE",
        "template": "/api/profiles/{name}",
        "url": "/api/profiles/nope",
        "status": 404,
    },
    {
        "label": "undo apply (no snapshots)",
        "method": "POST",
        "template": "/api/undo",
        "url": "/api/undo",
        "status": 404,
    },
    {
        "label": "search failure degrades",
        "method": "GET",
        "template": "/api/search",
        "url": "/api/search?q=harmony",
        "patches": {
            "rwmod.routers.workshop.search_workshop": lambda _q, page=1: (_ for _ in ()).throw(
                RuntimeError("offline")
            )
        },
        "status": 502,
    },
]

SUCCESS_CASES: list[dict] = [
    {"label": "status", "method": "GET", "template": "/api/status", "url": "/api/status"},
    {"label": "locale get", "method": "GET", "template": "/api/locale", "url": "/api/locale"},
    {
        "label": "locale set",
        "method": "POST",
        "template": "/api/locale",
        "url": "/api/locale",
        "kwargs": {"json": {"locale": "en"}},
    },
    {
        "label": "onboarding",
        "method": "GET",
        "template": "/api/onboarding/check",
        "url": "/api/onboarding/check",
    },
    {"label": "config get", "method": "GET", "template": "/api/config", "url": "/api/config"},
    {
        "label": "config set",
        "method": "POST",
        "template": "/api/config",
        "url": "/api/config",
        "kwargs": {"json": {"steam_api_key": "k"}},
    },
    {"label": "dashboard", "method": "GET", "template": "/api/dashboard", "url": "/api/dashboard"},
    {
        "label": "steamcmd probe",
        "method": "GET",
        "template": "/api/steamcmd/check",
        "url": "/api/steamcmd/check",
    },
    {"label": "mods list", "method": "GET", "template": "/api/mods", "url": "/api/mods"},
    {
        "label": "mods check-updates",
        "method": "GET",
        "template": "/api/mods/check-updates",
        "url": "/api/mods/check-updates",
    },
    {
        "label": "mods health",
        "method": "GET",
        "template": "/api/mods/health",
        "url": "/api/mods/health",
    },
    {
        "label": "mods export",
        "method": "GET",
        "template": "/api/mods/export",
        "url": "/api/mods/export",
    },
    {
        "label": "mods export-collection",
        "method": "GET",
        "template": "/api/mods/export-collection",
        "url": "/api/mods/export-collection",
    },
    {
        "label": "mods compatibility",
        "method": "GET",
        "template": "/api/mods/compatibility",
        "url": "/api/mods/compatibility",
    },
    {
        "label": "mods localization",
        "method": "GET",
        "template": "/api/mods/localization",
        "url": "/api/mods/localization",
    },
    {
        "label": "mods dependencies empty",
        "method": "POST",
        "template": "/api/mods/dependencies",
        "url": "/api/mods/dependencies",
        "kwargs": {"json": {"ids": []}},
    },
    {
        "label": "mod settings list",
        "method": "GET",
        "template": "/api/mod-settings",
        "url": "/api/mod-settings",
    },
    {
        "label": "mod settings backups",
        "method": "GET",
        "template": "/api/mod-settings/backups",
        "url": "/api/mod-settings/backups",
    },
    {
        "label": "mod settings backup all",
        "method": "POST",
        "template": "/api/mod-settings/backup-all",
        "url": "/api/mod-settings/backup-all",
    },
    {
        "label": "compat check",
        "method": "GET",
        "template": "/api/compat/check",
        "url": "/api/compat/check",
    },
    {
        "label": "search empty query",
        "method": "GET",
        "template": "/api/search",
        "url": "/api/search?q=",
    },
    {
        "label": "search mocked",
        "method": "GET",
        "template": "/api/search",
        "url": "/api/search?q=harmony",
        "patches": {
            "rwmod.routers.workshop.search_workshop": lambda _q, page=1: [
                __import__("rwmod.workshop", fromlist=["ModSearchResult"]).ModSearchResult(
                    id="7", title="T", author="A"
                )
            ]
        },
    },
    {
        "label": "workshop detail found",
        "method": "GET",
        "template": "/api/workshop/{mod_id}",
        "url": "/api/workshop/123",
        "patches": {
            "rwmod.routers.workshop.fetch_item_details": lambda ids: {
                i: {"title": "T", "time_updated": 0} for i in ids
            }
        },
    },
    {
        "label": "workshop detail missing",
        "method": "GET",
        "template": "/api/workshop/{mod_id}",
        "url": "/api/workshop/123",
        "patches": {"rwmod.routers.workshop.fetch_item_details": lambda _ids: {}},
    },
    {
        "label": "collection preview ok",
        "method": "GET",
        "template": "/api/collection/preview/{collection_id}",
        "url": "/api/collection/preview/123456",
        "patches": {
            "rwmod.routers.workshop.fetch_collection_children": lambda _cid: ["111", "222"]
        },
    },
    {"label": "queue state", "method": "GET", "template": "/api/queue", "url": "/api/queue"},
    {
        "label": "queue add",
        "method": "POST",
        "template": "/api/queue/add",
        "url": "/api/queue/add",
        "kwargs": {"json": {"ids": ["123456"]}},
    },
    {
        "label": "queue clear",
        "method": "POST",
        "template": "/api/queue/clear",
        "url": "/api/queue/clear",
    },
    # start() is stubbed out: a real worker makes the TestClient fixture's
    # lifespan shutdown raise (see API-5 / TestShutdownContract).
    {
        "label": "queue start",
        "method": "POST",
        "template": "/api/queue/start",
        "url": "/api/queue/start",
        "kwargs": {"json": {"force": False}},
        "patches": {"rwmod.queue.DownloadQueue.start": _async_method_noop},
    },
    {
        "label": "queue pause",
        "method": "POST",
        "template": "/api/queue/pause",
        "url": "/api/queue/pause",
    },
    {
        "label": "queue resume",
        "method": "POST",
        "template": "/api/queue/resume",
        "url": "/api/queue/resume",
    },
    {
        "label": "queue retry none",
        "method": "POST",
        "template": "/api/queue/retry",
        "url": "/api/queue/retry",
        "kwargs": {"json": {"ids": []}},
    },
    {"label": "backups list", "method": "GET", "template": "/api/backups", "url": "/api/backups"},
    {
        "label": "backups cleanup",
        "method": "POST",
        "template": "/api/backups/cleanup",
        "url": "/api/backups/cleanup",
        "kwargs": {"json": {"keep": 5}},
    },
    {"label": "history list", "method": "GET", "template": "/api/history", "url": "/api/history"},
    {
        "label": "history stats",
        "method": "GET",
        "template": "/api/history/stats",
        "url": "/api/history/stats",
    },
    {
        "label": "history clear",
        "method": "POST",
        "template": "/api/history/clear",
        "url": "/api/history/clear",
    },
    {
        "label": "profiles list",
        "method": "GET",
        "template": "/api/profiles",
        "url": "/api/profiles",
    },
    {
        "label": "profiles save",
        "method": "POST",
        "template": "/api/profiles/save",
        "url": "/api/profiles/save",
        "kwargs": {"json": {"name": "probe"}},
    },
    {"label": "tags list", "method": "GET", "template": "/api/tags", "url": "/api/tags"},
    {
        "label": "tags by tag",
        "method": "GET",
        "template": "/api/tags/by-tag/{tag}",
        "url": "/api/tags/by-tag/t1",
    },
    {
        "label": "tags get",
        "method": "GET",
        "template": "/api/tags/{mod_folder}",
        "url": "/api/tags/folder1",
    },
    {
        "label": "tags add",
        "method": "POST",
        "template": "/api/tags/{mod_folder}",
        "url": "/api/tags/folder1",
        "kwargs": {"json": {"tag": "t1"}},
    },
    {
        "label": "tags remove all",
        "method": "DELETE",
        "template": "/api/tags/{mod_folder}",
        "url": "/api/tags/folder1",
    },
    {
        "label": "rimsort generate",
        "method": "POST",
        "template": "/api/rimsort/generate",
        "url": "/api/rimsort/generate",
    },
    # No body: active_ids=None sorts everything installed. A non-installed
    # active_ids entry crashes sort_mods (API-3) — see the dedicated test.
    {
        "label": "rimsort sort",
        "method": "POST",
        "template": "/api/rimsort/sort",
        "url": "/api/rimsort/sort",
    },
    {
        "label": "rimsort compare-file",
        "method": "POST",
        "template": "/api/rimsort/compare-file",
        "url": "/api/rimsort/compare-file",
        "kwargs": {
            "files": {
                "file": (
                    "m.xml",
                    b"<ModsConfigData><activeMods><li>a.b</li></activeMods></ModsConfigData>",
                    "text/xml",
                )
            }
        },
    },
    {"label": "saves list", "method": "GET", "template": "/api/saves", "url": "/api/saves"},
    {
        "label": "saves analyze",
        "method": "POST",
        "template": "/api/saves/analyze",
        "url": "/api/saves/analyze",
        "kwargs": {
            "files": {
                "file": (
                    "s.rws",
                    b"<save><modIds><li>a.b</li><li>c.d</li></modIds></save>",
                    "application/octet-stream",
                )
            }
        },
    },
    {"label": "undo list", "method": "GET", "template": "/api/undo", "url": "/api/undo"},
    {
        "label": "auto-update status",
        "method": "GET",
        "template": "/api/auto-update/status",
        "url": "/api/auto-update/status",
    },
    {
        "label": "auto-check result",
        "method": "GET",
        "template": "/api/auto-check/result",
        "url": "/api/auto-check/result",
    },
    {
        "label": "auto-check clear",
        "method": "POST",
        "template": "/api/auto-check/clear",
        "url": "/api/auto-check/clear",
    },
    {
        "label": "auto-update run (mocked)",
        "method": "POST",
        "template": "/api/auto-update/run",
        "url": "/api/auto-update/run",
        "patches": {
            "rwmod.autoupdate.AutoUpdateManager.run_check": _async_noop_result(
                {"updates": [], "count": 0}
            )
        },
    },
    {
        "label": "download mocked",
        "method": "POST",
        "template": "/api/download",
        "url": "/api/download",
        "kwargs": {"json": {"ids": ["123456"]}},
        "patches": {"rwmod.routers.download.download_one": lambda *a, **k: True},
    },
    {
        "label": "import file mocked",
        "method": "POST",
        "template": "/api/import/file",
        "url": "/api/import/file",
        "kwargs": {"files": {"file": ("m.txt", b"123456\n# c\n654321\n", "text/plain")}},
        "patches": {"rwmod.routers.download.download_one": lambda *a, **k: True},
    },
    {
        "label": "import collection mocked",
        "method": "POST",
        "template": "/api/import/collection",
        "url": "/api/import/collection",
        "kwargs": {"json": {"collection_id": "123456"}},
        "patches": {
            "rwmod.routers.download.fetch_collection_children": lambda _cid: ["111", "222"],
            "rwmod.routers.download.download_batch": lambda _cfg, ids, **k: {i: True for i in ids},
        },
    },
    {
        "label": "import sort",
        "method": "POST",
        "template": "/api/import/sort",
        "url": "/api/import/sort",
        "kwargs": {
            "files": {
                "file": (
                    "ModsConfig.xml",
                    b"<ModsConfigData><activeMods><li>pkg.a</li></activeMods></ModsConfigData>",
                    "text/xml",
                )
            }
        },
    },
    {
        "label": "batch download mocked",
        "method": "POST",
        "template": "/api/mods/batch/download",
        "url": "/api/mods/batch/download",
        "kwargs": {"json": {"ids": ["123456"]}},
        "patches": {
            "rwmod.downloader.download_batch": lambda _cfg, ids, **k: {i: True for i in ids}
        },
    },
    {
        "label": "batch delete",
        "method": "POST",
        "template": "/api/mods/batch/delete",
        "url": "/api/mods/batch/delete",
        "kwargs": {"json": {"folders": ["../evil", "Nope"]}},
    },
    {
        "label": "download stream mocked",
        "method": "GET",
        "template": "/api/download/stream",
        "url": "/api/download/stream?id=123456",
        "patches": {
            "rwmod.routers.download.is_collection": lambda _mid: False,
            "rwmod.routers.download._find_existing": lambda *a, **k: None,
            "rwmod.routers.download.download_one": lambda *a, **k: True,
        },
    },
    {
        "label": "transfer import bundle",
        "method": "POST",
        "template": "/api/transfer/import",
        "url": "/api/transfer/import",
        "kwargs": {"files": None},
        "build_bundle": True,
    },
]


def _covered_ops() -> set[tuple[str, str]]:
    ops = {(c["method"], c["template"]) for c in FAILURE_CASES}
    ops |= {(c["method"], c["template"]) for c in SUCCESS_CASES}
    # Exercised by dedicated tests below rather than the tables.
    ops |= {
        ("GET", "/api/download/stream"),  # dedicated SSE tests
        ("POST", "/api/transfer/export"),  # binary body, dedicated test
        ("POST", "/api/rimsort/apply"),  # file mutation, dedicated test
        ("GET", "/api/rimsort/check-order"),  # dedicated test with a real file
        # Steam DB endpoints: dedicated tests mock the 47 MB download / cache.
        ("POST", "/api/rimsort/steamdb/refresh"),
        ("GET", "/api/rimsort/steamdb/status"),
        ("PUT", "/api/mod-settings/{filename}"),  # dedicated round-trip test
        ("GET", "/metrics"),  # text body, dedicated test
        ("GET", "/"),  # HTML body, dedicated test
        # Multi-instance creation needs an absolute temp path per test.
        ("POST", "/api/config/instances"),  # test_api.py::TestInstanceEndpoints
    }
    return ops


# ── 1. route inventory / coverage ──────────────────────────────────


class TestFixtureIsolation:
    """Guards the hard rule: no test may touch the developer's real data."""

    def test_backup_dir_points_into_tmp_path(self, cfg, tmp_path: Path):
        """Config() computes backup_dir from the *default* mods_dir, so a fixture
        that only reassigns cfg.mods_dir leaves backup_dir at D:/RimWorld/Mods/
        _backups — where backup_mod()/cleanup_backups() would really write."""
        assert cfg.backup_dir.is_relative_to(tmp_path), cfg.backup_dir
        assert cfg.mods_dir.is_relative_to(tmp_path)
        assert cfg.rimworld_dir.is_relative_to(tmp_path)

    def test_backup_write_stays_inside_tmp_path(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "IsolationProbe", workshop_id="424242")
        resp = client.post("/api/mods/batch/delete", json={"folders": ["IsolationProbe"]})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == ["IsolationProbe"]
        assert list(cfg.backup_dir.glob("*.zip")), "backup should land in the tmp dir"


class TestRouteInventory:
    def test_openapi_operation_count(self):
        from rwmod.server import app

        spec = app.openapi()
        ops = [
            (m.upper(), p)
            for p, operations in spec["paths"].items()
            for m in operations
            if m in ("get", "post", "put", "delete", "patch")
        ]
        assert len(ops) >= 70, f"route surface shrank unexpectedly: {len(ops)}"

    def test_every_route_is_exercised_by_the_probe_tables(self):
        """No endpoint is silently skipped by the contract sweep."""
        from rwmod.server import app

        spec = app.openapi()
        all_ops = {
            (m.upper(), p)
            for p, operations in spec["paths"].items()
            for m in operations
            if m in ("get", "post", "put", "delete", "patch")
        }
        uncovered = all_ops - _covered_ops()
        assert not uncovered, f"routes not probed: {sorted(uncovered)}"


# ── 2. failure contract + 3. 200 bodies carry data only ────────────


class TestFailureContract:
    @pytest.mark.parametrize("case", FAILURE_CASES, ids=lambda c: c["label"])
    def test_failure_shape(self, client: TestClient, case: dict):
        resp = _request(client, case)
        body = _assert_error(resp, case["status"])
        # A FastAPI-default body would be {"detail": [...]}; our handler always
        # emits a string, and never a list.
        assert not isinstance(body["detail"], list)

    def test_422_is_not_fastapi_default(self, client: TestClient):
        resp = client.post("/api/queue/add", json={"ids": 5})
        body = _assert_error(resp, 422)
        assert body["error"] == "ValidationError"
        assert "list" in body["detail"]

    def test_422_detail_flattens_every_field_error(self, client: TestClient):
        resp = client.post("/api/queue/add", json={"ids": [1, 2]})
        body = _assert_error(resp, 422)
        assert "ids.0" in body["detail"] and "ids.1" in body["detail"]

    def test_404_route_uses_the_standard_shape(self, client: TestClient):
        body = _assert_error(client.get("/api/definitely-not-a-route"), 404)
        assert body["error"] == "HTTPError"

    def test_405_uses_the_standard_shape(self, client: TestClient):
        body = _assert_error(client.get("/api/queue/clear"), 405)
        assert body["error"] == "HTTPError"

    def test_errors_py_exception_keeps_its_class_name(self, client: TestClient):
        """`error` names the errors.py class when one was actually raised."""
        body = _assert_error(client.get("/api/saves/nope"), 404)
        assert body["error"] == "ModNotFoundError"

    def test_validation_error_from_errors_py_is_400(self, client: TestClient, cfg):
        """load_order raises errors.ValidationError (400), not a 200 body."""
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<ModsConfigData><activeMods/></ModsConfigData>", encoding="utf-8")
        body = _assert_error(client.get("/api/rimsort/check-order"), 400)
        assert body["error"] == "ValidationError"

    def test_unhandled_exception_gets_the_500_fallback(self, client: TestClient, cfg):
        """A crash answers {error: InternalError, detail: str} — no traceback leak."""
        import rwmod.routers.mods as mods_router

        with patch.object(
            mods_router,
            "get_cached_mods",
            side_effect=RuntimeError("boom"),
        ):
            mods_router._mods_cache.clear()
            client._transport.raise_server_exceptions = False  # type: ignore[attr-defined]
            try:
                resp = client.get("/api/mods")
            finally:
                client._transport.raise_server_exceptions = True  # type: ignore[attr-defined]
        body = _assert_error(resp, 500)
        assert body["error"] == "InternalError"
        assert "boom" not in body["detail"]

    def test_probe_endpoint_may_answer_ok_msg(self, client: TestClient):
        """The one documented exception: a probe reporting a verdict."""
        resp = client.get("/api/steamcmd/check")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"ok", "msg"}
        assert isinstance(body["ok"], bool)
        assert isinstance(body["msg"], str)


class TestSuccessBodiesCarryNoErrorKey:
    @pytest.mark.parametrize("case", SUCCESS_CASES, ids=lambda c: c["label"])
    def test_200_has_no_error_key(self, client: TestClient, cfg, case: dict):
        if case.get("build_bundle"):
            from rwmod.transfer import export_bundle

            bundle = cfg.mods_dir.parent / "probe.rwmod"
            export_bundle(cfg, bundle, include_backups=False)
            resp = client.post(
                "/api/transfer/import",
                files={"file": ("probe.rwmod", bundle.read_bytes(), "application/zip")},
            )
            _assert_200_data(resp)
            return

        resp = _request(client, case)
        assert resp.status_code == 200, f"{case['label']}: {resp.status_code} {resp.text[:200]}"
        if resp.headers.get("content-type", "").startswith("application/json"):
            body = resp.json()
            if isinstance(body, dict):
                assert "error" not in body, f"{case['label']} 200 body has error key: {body}"

    def test_probe_degrades_are_not_error_keys(self, client: TestClient):
        """Documented degraded successes keep `error` out of the 200 body."""
        assert _assert_200_data(client.get("/api/mods/compatibility")) == {
            "rimworld_version": None,
            "groups": {},
        }


# ── 4. auth posture (matrix in the task brief does not match this revision) ──


class TestAuthPosture:
    def test_no_endpoint_answers_401_or_403(self, client: TestClient):
        """This revision has no auth layer at all — not localhost-gated, no JWT,
        no RWMOD_SECRET. Every route is reachable anonymously."""
        from rwmod.server import app

        spec = app.openapi()
        ops = [
            (m.upper(), p)
            for p, operations in spec["paths"].items()
            for m in operations
            if m in ("get", "post", "delete")
            # Skip the ones that need network mocks / fixtures; status code is
            # what matters here and they answer 200/4xx without auth anyway.
        ]
        offenders = []
        with (
            patch("rwmod.routers.workshop.search_workshop", return_value=[]),
            patch("rwmod.routers.workshop.fetch_item_details", return_value={}),
            patch("rwmod.routers.workshop.fetch_collection_children", return_value=[]),
            patch(
                "rwmod.autoupdate.AutoUpdateManager.run_check",
                _async_noop_result({"updates": [], "count": 0}),
            ),
            patch("rwmod.queue.DownloadQueue.start", _async_method_noop),
            # The refresh endpoint's whole job is a 47 MB download; the auth
            # sweep only cares about status codes, so stub the download.
            patch("rwmod.routers.rimsort.SteamDB.refresh", return_value=True),
        ):
            for method, path in ops:
                url = path.replace("{save_name}", "x").replace("{mod_id}", "1")
                url = url.replace("{collection_id}", "1").replace("{mod_folder}", "f")
                url = url.replace("{tag}", "t").replace("{filename}", "f.zip")
                url = url.replace("{name}", "n").replace("{workshop_id}", "1")
                resp = client.request(method, url)
                if resp.status_code in (401, 403):
                    offenders.append((method, path, resp.status_code))
        assert not offenders, f"unexpected auth gate appeared: {offenders}"

    def test_non_local_client_is_not_challenged(self, client: TestClient):
        """A request whose ASGI client address is a LAN IP is served like any
        other — there is no localhost-only gate to verify."""
        client._transport.client = ("10.0.0.5", 51000)  # type: ignore[attr-defined]
        try:
            resp = client.get("/api/status")
            assert resp.status_code == 200
            resp = client.get("/api/config")
            assert resp.status_code == 200
        finally:
            client._transport.client = ("testclient", 50000)  # type: ignore[attr-defined]

    def test_secret_env_var_does_not_gate_localhost(self, client: TestClient, monkeypatch):
        """Setting RWMOD_SECRET neither exists nor changes anything — pinning the
        current (documented-as-intentional) open posture so a future gate is a
        deliberate, visible change."""
        monkeypatch.setenv("RWMOD_SECRET", "hunter2")
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/config").status_code == 200

    def test_websocket_needs_no_token(self, client: TestClient):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json()["type"] == "pong"


# ── 5. SSE ─────────────────────────────────────────────────────────


def _sse_events(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        assert block.startswith("data: "), f"non-data SSE block: {block!r}"
        events.append(json.loads(block[len("data: ") :]))
    return events


class TestSSE:
    def test_stream_is_well_formed_and_data_only(self, client: TestClient):
        with (
            patch("rwmod.routers.download.is_collection", return_value=False),
            patch("rwmod.routers.download._find_existing", return_value=None),
            patch("rwmod.routers.download.download_one", return_value=True),
        ):
            resp = client.get("/api/download/stream?id=123456")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.text.endswith("\n\n")
        events = _sse_events(resp.text)
        assert [e["event"] for e in events] == ["start", "ok", "done"]
        assert all("error" not in e for e in events)
        assert events[0]["id"] == "123456"

    def test_stream_installed_mod_skips(self, client: TestClient, cfg):
        with (
            patch("rwmod.routers.download.is_collection", return_value=False),
            patch("rwmod.routers.download._find_existing", return_value=cfg.mods_dir / "already"),
        ):
            resp = client.get("/api/download/stream?id=123456")
        assert [e["event"] for e in _sse_events(resp.text)] == ["start", "skip", "done"]
        assert "skip" in resp.text

    def test_stream_failure_is_a_fail_event_not_an_http_error(self, client: TestClient):
        """The 200 was already sent, so a failed download reports via `fail`."""
        with (
            patch("rwmod.routers.download.is_collection", return_value=False),
            patch("rwmod.routers.download._find_existing", return_value=None),
            patch("rwmod.routers.download.download_one", return_value=False),
        ):
            resp = client.get("/api/download/stream?id=999")
        events = _sse_events(resp.text)
        assert [e["event"] for e in events] == ["start", "fail", "done"]

    def test_stream_force_on_installed_emits_info_then_result(self, client: TestClient, cfg):
        with (
            patch("rwmod.routers.download.is_collection", return_value=False),
            patch("rwmod.routers.download._find_existing", return_value=cfg.mods_dir / "already"),
            patch("rwmod.routers.download.download_one", return_value=True),
        ):
            resp = client.get("/api/download/stream?id=123456&force=true")
        events = [e["event"] for e in _sse_events(resp.text)]
        assert events == ["start", "info", "ok", "done"]

    def test_collection_stream_event_set(self, client: TestClient):
        with (
            patch("rwmod.routers.download.is_collection", return_value=True),
            patch("rwmod.routers.download.fetch_collection_children", return_value=["111", "222"]),
            patch(
                "rwmod.routers.download.download_batch",
                side_effect=lambda _cfg, ids, **k: {ids[0]: True, ids[-1]: False},
            ),
        ):
            resp = client.get("/api/download/stream?id=123456")

        events = _sse_events(resp.text)
        names = [e["event"] for e in events]
        assert names[0] == "start"
        assert names[-1] == "done"
        assert {"info", "ok", "warn"}.issubset(set(names))
        assert all(e.get("msg") or e.get("id") or e.get("event") for e in events)

    def test_collection_stream_unfetchable_warns_and_closes(self, client: TestClient):
        with (
            patch("rwmod.routers.download.is_collection", return_value=True),
            patch("rwmod.routers.download.fetch_collection_children", return_value=[]),
        ):
            resp = client.get("/api/download/stream?id=123456")
        assert [e["event"] for e in _sse_events(resp.text)] == ["start", "info", "warn", "done"]

    def test_collection_stream_skips_installed_children(self, client: TestClient, cfg):
        """Installed children are reported as ONE summary event.

        Per-item `skip` events used to flood the panel with hundreds of lines
        for a large collection.
        """
        _make_mod(cfg.mods_dir, "InstalledMod", workshop_id="111")
        with (
            patch("rwmod.routers.download.is_collection", return_value=True),
            patch("rwmod.routers.download.fetch_collection_children", return_value=["111"]),
            patch("rwmod.routers.download.download_batch", return_value={}),
        ):
            resp = client.get("/api/download/stream?id=123456")
        events = _sse_events(resp.text)
        assert not any(e["event"] == "skip" for e in events)
        summary = [e for e in events if e["event"] == "info" and e.get("skipped") == 1]
        assert summary and "已安装" in summary[0]["msg"]

    def test_stream_never_emits_comment_heartbeats(self, client: TestClient):
        """Heartbeats inside a batch are `data:` info events, and the solo-download
        heartbeat is swallowed by _drain_heartbeat — no `:` comment lines exist."""
        with (
            patch("rwmod.routers.download.is_collection", return_value=False),
            patch("rwmod.routers.download._find_existing", return_value=None),
            patch("rwmod.routers.download.download_one", return_value=True),
        ):
            resp = client.get("/api/download/stream?id=123456")
        assert not [ln for ln in resp.text.splitlines() if ln.startswith(":")]

    def test_stream_bad_id_is_a_normal_error_response(self, client: TestClient):
        body = _assert_error(client.get("/api/download/stream?id=not-a-number"), 400)
        assert body["error"] == "HTTPError"


# ── 6. WebSocket ───────────────────────────────────────────────────


class TestWebSocket:
    def test_ping_pong(self, client: TestClient):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"cmd": "ping"})
            msg = ws.receive_json()
        assert msg["type"] == "pong"
        assert isinstance(msg["queue_pending"], int)

    def test_default_command_is_ping(self, client: TestClient):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({})
            assert ws.receive_json()["type"] == "pong"

    def test_subscribe_echoes_topic(self, client: TestClient):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"cmd": "subscribe", "topic": "queue"})
            assert ws.receive_json() == {"type": "subscribed", "topic": "queue"}

    def test_subscribe_without_topic_defaults_to_all(self, client: TestClient):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"cmd": "subscribe"})
            assert ws.receive_json() == {"type": "subscribed", "topic": "all"}

    def test_unknown_command_echoes(self, client: TestClient):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"cmd": "dance"})
            assert ws.receive_json() == {"type": "echo", "cmd": "dance"}

    def test_invalid_json_answers_error_message(self, client: TestClient):
        with client.websocket_connect("/ws") as ws:
            ws.send_text("{not json")
            assert ws.receive_json() == {"type": "error", "msg": "invalid json"}
            # The socket stays usable afterwards.
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_subprotocol_token_is_not_verified(self, client: TestClient):
        """No `rwmod.<token>` handshake exists: any (or no) subprotocol connects,
        and the server never selects one."""
        with client.websocket_connect("/ws", subprotocols=["rwmod.deadbeef"]) as ws:
            assert ws.accepted_subprotocol is None
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_bogus_subprotocol_is_not_rejected(self, client: TestClient):
        with client.websocket_connect("/ws", subprotocols=["definitely-not-a-token"]) as ws:
            ws.send_json({"cmd": "ping"})
            assert ws.receive_json()["type"] == "pong"


# ── 7. security headers / CORS ─────────────────────────────────────


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        "method,url,kwargs",
        [
            ("GET", "/api/status", {}),
            ("GET", "/api/nonexistent", {}),
            ("POST", "/api/queue/add", {"json": {"ids": 5}}),
        ],
        ids=["200", "404", "422"],
    )
    def test_api_headers(self, client: TestClient, method, url, kwargs):
        resp = client.request(method, url, **kwargs)
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-request-id"]

    def test_non_api_response_has_no_store_excluded(self, client: TestClient):
        resp = client.get("/")
        # 503 is the documented response when static/ is absent ("frontend not built"),
        # which is the normal state on a CI runner that never built the frontend.
        assert resp.status_code in (200, 503), resp.text
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert "no-store" not in resp.headers.get("cache-control", "")

    def test_request_id_is_echoed(self, client: TestClient):
        resp = client.get("/api/status", headers={"X-Request-ID": "probe-1234"})
        assert resp.headers["x-request-id"] == "probe-1234"

    @pytest.mark.parametrize(
        "origin",
        ["http://localhost:5173", "https://localhost", "http://127.0.0.1:8000"],
    )
    def test_cors_allows_localhost_origins(self, client: TestClient, origin):
        resp = client.get("/api/status", headers={"Origin": origin})
        assert resp.headers["access-control-allow-origin"] == origin
        assert resp.headers["access-control-allow-credentials"] == "true"

    @pytest.mark.parametrize(
        "origin",
        ["http://localhost.evil.com", "http://127.0.0.1.evil.com", "http://evil.com"],
    )
    def test_cors_rejects_lookalike_origins(self, client: TestClient, origin):
        resp = client.get("/api/status", headers={"Origin": origin})
        assert "access-control-allow-origin" not in resp.headers

    def test_cors_preflight_allowed(self, client: TestClient):
        resp = client.options(
            "/api/queue/add",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_cors_preflight_rejected(self, client: TestClient):
        resp = client.options(
            "/api/queue/add",
            headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in resp.headers


# ── 8. malformed / hostile input tolerance ─────────────────────────


class TestMalformedInput:
    def test_malformed_json_is_422_not_500(self, client: TestClient):
        resp = client.post(
            "/api/queue/add", content=b'{"ids": [', headers={"content-type": "application/json"}
        )
        _assert_error(resp, 422)

    def test_invalid_utf8_body_is_a_4xx_error_body(self, client: TestClient):
        resp = client.post(
            "/api/queue/add", content=b"\xff\xfe\x00", headers={"content-type": "application/json"}
        )
        assert resp.status_code in (400, 422), resp.text
        _assert_error(resp, resp.status_code)

    def test_wrong_content_type_is_handled(self, client: TestClient):
        """A JSON body under a non-JSON content type must not crash the server."""
        resp = client.post(
            "/api/queue/add", content=b'{"ids": ["1"]}', headers={"content-type": "text/plain"}
        )
        assert resp.status_code in (200, 415, 422), resp.text
        if resp.status_code != 200:
            _assert_error(resp, resp.status_code)

    def test_large_json_body_is_accepted(self, client: TestClient):
        ids = [f"{i:05d}" + "1" * 251 for i in range(1500)]  # ~400 KB of JSON
        resp = client.post("/api/queue/add", json={"ids": ids})
        assert resp.status_code == 200
        assert resp.json()["added"] == len(ids)

    def test_long_path_parameter_does_not_500(self, client: TestClient):
        resp = client.get("/api/tags/by-tag/" + "a" * 8000)
        assert resp.status_code == 200
        assert resp.json()["tag"] == "a" * 8000

    def test_sql_injection_path_parameter_is_bound(self, client: TestClient):
        resp = client.get("/api/tags/by-tag/" + "'; DROP TABLE mod_tags;--")
        assert resp.status_code == 200
        # The table must still exist and the API must still work.
        assert client.get("/api/tags").status_code == 200

    def test_unicode_path_parameter(self, client: TestClient):
        resp = client.get("/api/tags/by-tag/%E4%B8%AD%E6%96%87")
        assert resp.status_code == 200

    def test_nul_byte_path_does_not_500(self, client: TestClient):
        resp = client.get("/api/tags/a%00b")
        assert resp.status_code < 500

    def test_extreme_query_limit_does_not_500(self, client: TestClient):
        resp = client.get("/api/history?limit=99999999999999999999")
        assert resp.status_code < 500, resp.text
        _assert_error(resp, resp.status_code)

    def test_negative_query_limit_is_tolerated(self, client: TestClient):
        assert client.get("/api/history?limit=-5").status_code == 200

    def test_oversized_multipart_is_413(self, client: TestClient):
        resp = client.post(
            "/api/import/file",
            files={"file": ("m.txt", b"x" * (5 * 1024 * 1024 + 128), "text/plain")},
        )
        _assert_error(resp, 413)

    def test_oversized_mod_zip_name_only_is_400(self, client: TestClient):
        """End-to-end: a non-zip upload never reaches extraction."""
        resp = client.post(
            "/api/mods/import-local", files={"file": ("mod.zip", b"", "application/zip")}
        )
        _assert_error(resp, 400)


# ── 9. router branch coverage (baseline priority: download/mods/rimsort/workshop) ──


class TestModsRouterBranches:
    def test_list_mods_reports_size_and_tags(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "TaggedMod", package_id="t.m", workshop_id="555")
        (cfg.mods_dir / "TaggedMod" / "Defs").mkdir()
        (cfg.mods_dir / "TaggedMod" / "Defs" / "big.bin").write_bytes(b"0" * 2048)
        assert client.post("/api/tags/TaggedMod", json={"tag": "qol"}).status_code == 200

        mods = _assert_200_data(client.get("/api/mods"))
        entry = next(m for m in mods if m["folder"] == "TaggedMod")
        assert entry["tags"] == ["qol"]
        assert entry["size_mb"] >= 0.0
        assert entry["workshop_id"] == "555"

    def test_import_local_zip_roundtrip(self, client: TestClient, cfg):
        resp = client.post(
            "/api/mods/import-local",
            files={"file": ("m.zip", _zip_with_mod("ZipMod"), "application/zip")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["count"] == 1
        assert (cfg.mods_dir / "ZipMod" / "About" / "About.xml").exists()

    def test_import_local_zip_without_mod_metadata_400(self, client: TestClient):
        resp = client.post(
            "/api/mods/import-local",
            files={
                "file": ("m.zip", _zip_with_mod("Junk", include_about=False), "application/zip")
            },
        )
        _assert_error(resp, 400)

    def test_batch_delete_splits_deleted_and_rejected(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Doomed", workshop_id="777")
        resp = client.post(
            "/api/mods/batch/delete",
            json={"folders": ["Doomed", "../escape", ".", "missing"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] == ["Doomed"]
        assert len(body["failed"]) == 3
        assert not (cfg.mods_dir / "Doomed").exists()

    def test_batch_delete_rejects_drive_relative_and_stream_names(self, client: TestClient, cfg):
        """Drive-relative paths and NTFS stream names have no separator but
        still escape mods_dir / name a stream — reject them before any
        filesystem call.

        ``C:Windows`` deliberately names the real system directory: only the
        response structure is asserted here, and the endpoint must classify it
        as 非法文件夹名 so no rmtree is ever pointed at it.
        """
        _make_mod(cfg.mods_dir, "normal_mod", workshop_id="888")

        resp = client.post(
            "/api/mods/batch/delete",
            json={"folders": ["C:Windows", "D:", "mod:stream", "normal_mod"]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] == ["normal_mod"]
        assert body["count"] == 1
        assert body["failed"] == [
            {"folder": "C:Windows", "reason": "非法文件夹名"},
            {"folder": "D:", "reason": "非法文件夹名"},
            {"folder": "mod:stream", "reason": "非法文件夹名"},
        ]
        # The system directory was only ever named, never touched.
        assert "C:Windows" not in body["deleted"]
        assert not (cfg.mods_dir / "normal_mod").exists()

    def test_batch_download_mocked(self, client: TestClient):
        with patch(
            "rwmod.downloader.download_batch",
            side_effect=lambda _cfg, ids, **k: {i: True for i in ids},
        ):
            resp = client.post("/api/mods/batch/download", json={"ids": ["123456"]})
        assert resp.status_code == 200
        assert resp.json()["results"] == [{"id": "123456", "ok": True}]

    def test_health_status_buckets(self, client: TestClient, cfg):
        now = int(time.time())
        _make_mod(cfg.mods_dir, "Fresh", workshop_id="1")
        _make_mod(cfg.mods_dir, "Stale", workshop_id="2")
        _make_mod(cfg.mods_dir, "Old", workshop_id="3")
        _make_mod(cfg.mods_dir, "Gone", workshop_id="4")
        details = {
            "1": {"time_updated": now - 5 * 86400},
            "2": {"time_updated": now - 200 * 86400},
            "3": {"time_updated": now - 400 * 86400},
        }

        # Health is cache-first: the first request answers immediately and
        # refreshes in the background. The patch point moved from the router to
        # the persistence module (per-batch fetch, so a failed batch keeps its
        # old rows instead of mass-marking mods removed).
        import rwmod.mod_health_check as health_mod

        def fake_fetch(ids: list[str]) -> dict:
            return {wid: details[wid] for wid in ids if wid in details}

        with patch("rwmod.mod_health_check._fetch_batch", side_effect=fake_fetch):
            _assert_200_data(client.get("/api/mods/health"))
            assert health_mod.wait_for_refresh(cfg.mods_dir, timeout=5.0) is True
            body = _assert_200_data(client.get("/api/mods/health"))
        by_folder = {m["folder"]: m["status"] for m in body["mods"]}
        assert by_folder == {
            "Fresh": "maintained",
            "Stale": "stale",
            "Old": "abandoned",
            "Gone": "removed",
        }
        assert by_folder  # last_updated formatted only for known entries
        fresh = next(m for m in body["mods"] if m["folder"] == "Fresh")
        assert fresh["last_updated"]

    def test_localization_detects_chinese_folder(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "CnMod", workshop_id="9", chinese=True)
        _make_mod(cfg.mods_dir, "EnMod", workshop_id="10")
        body = _assert_200_data(client.get("/api/mods/localization"))
        flags = {m["folder"]: m["has_chinese"] for m in body["mods"]}
        assert flags == {"CnMod": True, "EnMod": False}

    def test_export_collection_lists_workshop_ids(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Exported", workshop_id="4242")
        body = _assert_200_data(client.get("/api/mods/export-collection"))
        assert body["ids"] == ["4242"]
        assert "steamcommunity.com" in body["mods"][0]["url"]

    def test_dependencies_resolves_installed_and_remote(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Dep", package_id="dep.one", workshop_id="900")
        # fetch_item_details is imported into the router namespace, so it must be
        # patched there (patching rwmod.workshop would leave the real, networked
        # binding in place).
        with (
            patch("rwmod.workshop.fetch_item_dependencies", return_value={"500": ["900", "901"]}),
            patch(
                "rwmod.routers.workshop.fetch_item_details",
                return_value={"901": {"title": "Remote"}},
            ),
        ):
            body = _assert_200_data(client.post("/api/mods/dependencies", json={"ids": ["500"]}))
        deps = body["deps"]["500"]
        assert deps[0]["installed"] is True
        assert deps[1] == {"id": "901", "name": "Remote", "installed": False}


class TestModSettingsRouterBranches:
    """PUT/restore need a real Config dir, so they run here, not in the tables."""

    @pytest.fixture(autouse=True)
    def _sandbox_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RWMOD_TEST_SANDBOX_HOME", str(tmp_path))

    @staticmethod
    def _use_config_dir(cfg, tmp_path: Path) -> Path:
        config_dir = tmp_path / "probe_config"
        config_dir.mkdir()
        cfg.config_dir = config_dir
        cfg.save()
        return config_dir

    def test_put_and_restore_roundtrip(self, client: TestClient, cfg, tmp_path: Path):
        config_dir = self._use_config_dir(cfg, tmp_path)
        target = config_dir / "Mod_1_Thing.xml"
        target.write_text("<v1/>", encoding="utf-8")
        assert target.is_relative_to(tmp_path)

        saved = client.put(
            "/api/mod-settings/Mod_1_Thing.xml",
            json={"xml": "<v2/>", "ack_game_closed": True},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["ok"] is True
        assert target.read_text(encoding="utf-8") == "<v2/>"

        restored = client.post(
            "/api/mod-settings/Mod_1_Thing.xml/restore",
            json={"ack_game_closed": True},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["restored_from"]
        assert target.read_text(encoding="utf-8") == "<v1/>"


class TestDownloadRouterBranches:
    def test_import_file_parses_comments_and_urls(self, client: TestClient, cfg):
        payload = (
            b"# comment\n123456\n"
            b"https://steamcommunity.com/sharedfiles/filedetails/?id=654321\nnonsense\n"
        )
        with patch("rwmod.routers.download.download_one", return_value=True):
            body = _assert_200_data(
                client.post(
                    "/api/import/file",
                    files={"file": ("mods.txt", payload, "text/plain")},
                )
            )
        assert body["total"] == 2

    def test_import_collection_mocked_roundtrip(self, client: TestClient):
        with (
            patch("rwmod.routers.download.fetch_collection_children", return_value=["111", "222"]),
            patch(
                "rwmod.routers.download.download_batch",
                side_effect=lambda _cfg, ids, **k: {i: True for i in ids},
            ),
        ):
            body = _assert_200_data(
                client.post("/api/import/collection", json={"collection_id": "123456"})
            )
        assert body["total"] == 2
        assert all(r["ok"] for r in body["results"])

    def test_import_collection_drains_extra_dependencies(self, client: TestClient):
        """Sub-collections discovered mid-batch are appended and downloaded."""
        calls: list[list[str]] = []

        def _fake_batch(_cfg, ids, force=False, extra_out=None):
            calls.append(list(ids))
            if extra_out is not None and len(calls) == 1:
                extra_out.append("999")
            return {i: True for i in ids}

        with (
            patch("rwmod.routers.download.fetch_collection_children", return_value=["111"]),
            patch("rwmod.routers.download.download_batch", side_effect=_fake_batch),
        ):
            body = _assert_200_data(
                client.post("/api/import/collection", json={"collection_id": "123456"})
            )
        assert body["total"] == 2
        assert calls == [["111"], ["999"]]

    def test_import_sort_reports_unknown_packages(self, client: TestClient):
        xml = (
            b"<ModsConfigData><activeMods><li>pkg.a</li><li>pkg.b</li></activeMods>"
            b"</ModsConfigData>"
        )
        with patch("rwmod.routers.download.download_one", return_value=True):
            body = _assert_200_data(
                client.post("/api/import/sort", files={"file": ("ModsConfig.xml", xml, "text/xml")})
            )
        assert body["total_packages"] == 2
        assert body["missing"] == 2
        assert set(body["unknown"]) == {"pkg.a", "pkg.b"}

    def test_import_sort_missing_ids_are_reported_not_crashed(self, client: TestClient, cfg):
        """An installed mod is never "missing", so the resolver has nothing to
        map and every missing package lands in `unknown` (see report API-6/API-7)."""
        _make_mod(cfg.mods_dir, "Known", package_id="pkg.a", workshop_id="321")
        xml = b"<ModsConfigData><activeMods><li>pkg.b</li></activeMods></ModsConfigData>"
        with patch("rwmod.routers.download.download_one", return_value=True) as dl:
            body = _assert_200_data(
                client.post("/api/import/sort", files={"file": ("ModsConfig.xml", xml, "text/xml")})
            )
        assert body["missing"] == 1
        assert body["unknown"] == ["pkg.b"]
        assert dl.called is False
        # Contract shape holds even though nothing could be resolved.
        assert set(body) >= {"total_packages", "missing", "unknown", "downloaded", "results"}

    def test_force_overwrite_stream_finds_final_name(self, client: TestClient, cfg):
        target = _make_mod(cfg.mods_dir, "Overwritten", workshop_id="123456")
        with (
            patch("rwmod.routers.download.is_collection", return_value=False),
            patch("rwmod.routers.download._find_existing", return_value=target),
            patch("rwmod.routers.download.download_one", return_value=True),
        ):
            resp = client.get("/api/download/stream?id=123456&force=true")
        events = _sse_events(resp.text)
        ok = next(e for e in events if e["event"] == "ok")
        assert ok["msg"] == "Overwritten"


class TestRimsortRouterBranches:
    def test_compare_reports_missing_and_extra(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Installed", package_id="pkg.installed")
        xml = b"<ModsConfigData><activeMods><li>pkg.missing</li></activeMods></ModsConfigData>"
        body = _assert_200_data(client.post("/api/rimsort/compare", json={"xml": xml.decode()}))
        assert body["missing"] == ["pkg.missing"]
        assert body["extra"] == ["pkg.installed"]
        assert body["missing_details"][0]["package_id"] == "pkg.missing"

    def test_compare_malformed_xml_must_be_an_error_response(self, client: TestClient):
        resp = client.post("/api/rimsort/compare", json={"xml": "<broken><unclosed>"})
        assert resp.status_code >= 400
        assert set(resp.json()) == {"error", "detail"}

    def test_compare_file_malformed_must_be_an_error_response(self, client: TestClient):
        resp = client.post(
            "/api/rimsort/compare-file",
            files={"file": ("m.xml", b"definitely not xml", "text/xml")},
        )
        assert resp.status_code >= 400
        assert set(resp.json()) == {"error", "detail"}

    def test_apply_rewrites_modsconfig_and_backs_up(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Sorted", package_id="pkg.sorted")
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "<ModsConfigData><activeMods><li>pkg.sorted</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        body = _assert_200_data(client.post("/api/rimsort/apply"))
        assert body["ok"] is True
        assert body["backup"]
        assert "pkg.sorted" in body["sorted"]
        assert list(cfg.rimworld_dir.glob("ModsConfig.xml.bak_*"))

    def test_apply_without_modsconfig_is_404(self, client: TestClient):
        _assert_error(client.post("/api/rimsort/apply"), 404)

    def test_apply_without_active_mods_is_400(self, client: TestClient, cfg):
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "<ModsConfigData><version>1.0</version></ModsConfigData>", encoding="utf-8"
        )
        _assert_error(client.post("/api/rimsort/apply"), 400)

    def test_apply_malformed_modsconfig_is_400(self, client: TestClient, cfg):
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<ModsConfigData><activeMods>", encoding="utf-8")
        _assert_error(client.post("/api/rimsort/apply"), 400)

    def test_sort_with_installed_active_ids(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Sortable", package_id="pkg.sortable")
        body = _assert_200_data(
            client.post("/api/rimsort/sort", json={"active_ids": ["pkg.sortable"]})
        )
        assert body["sorted"] == ["pkg.sortable"]

    def test_sort_with_unknown_active_id_must_not_500(self, client: TestClient):
        resp = client.post("/api/rimsort/sort", json={"active_ids": ["not.installed"]})
        assert resp.status_code < 500, resp.text
        _assert_error(resp, resp.status_code)

    def test_check_order_reports_issues(self, client: TestClient, cfg):
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "<ModsConfigData><activeMods><li>ludeon.rimworld</li><li>brrainz.harmony</li>"
            "</activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        body = _assert_200_data(client.get("/api/rimsort/check-order"))
        assert body["total_mods"] == 2
        assert any(i["severity"] == "error" for i in body["issues"])

    def test_sort_uses_cached_community_rules(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Aaa", package_id="a.mod")
        _make_mod(cfg.mods_dir, "Bbb", package_id="b.mod")
        cache_dir = Path.home() / ".rwmod"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "community_rules.json").write_text(
            json.dumps({"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}}),
            encoding="utf-8",
        )

        body = _assert_200_data(
            client.post("/api/rimsort/sort", json={"active_ids": ["a.mod", "b.mod"]})
        )

        assert body["sorted"] == ["b.mod", "a.mod"]

    def test_sort_refresh_rules_flag_calls_refresh(
        self, client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.routers.rimsort as rimsort_router

        calls: list[bool] = []

        def fake_refresh() -> bool:
            calls.append(True)
            return True  # the flag now distinguishes success from a failed refresh

        monkeypatch.setattr(rimsort_router, "refresh_community_rules", fake_refresh)
        _make_mod(cfg.mods_dir, "Aaa", package_id="a.mod")

        _assert_200_data(
            client.post("/api/rimsort/sort?refresh_rules=true", json={"active_ids": ["a.mod"]})
        )
        assert calls == [True]

    def test_check_order_reports_declared_rule_violation(self, client: TestClient, cfg):
        _make_mod(
            cfg.mods_dir,
            "Aaa",
            package_id="a.mod",
            name="Aaa",
        )
        about = cfg.mods_dir / "Aaa" / "About" / "About.xml"
        about.write_text(
            "<ModMetaData><name>Aaa</name><packageId>a.mod</packageId>"
            "<loadAfter><li>b.mod</li></loadAfter></ModMetaData>",
            encoding="utf-8",
        )
        _make_mod(cfg.mods_dir, "Bbb", package_id="b.mod")
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "<ModsConfigData><version>1.6.4850 rev645</version><activeMods>"
            "<li>brrainz.harmony</li><li>a.mod</li><li>b.mod</li>"
            "</activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        body = _assert_200_data(client.get("/api/rimsort/check-order"))

        assert any("a.mod" in i["message"] and "b.mod" in i["message"] for i in body["issues"])

    def test_compat_reports_user_rule_conflicts(self, client: TestClient, cfg):
        _make_mod(cfg.mods_dir, "Aaa", package_id="a.mod")
        _make_mod(cfg.mods_dir, "Bbb", package_id="b.mod")
        rules_dir = Path.home() / ".rwmod"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "rules.json").write_text(
            json.dumps({"conflicts": [{"a": "a.mod", "b": "b.mod", "reason": "用户禁配"}]}),
            encoding="utf-8",
        )

        body = _assert_200_data(client.get("/api/compat/check"))

        assert any("用户禁配" in c["reason"] for c in body["conflicts"])
        assert body["ok"] is False


class TestSteamDbEndpoints:
    """Opt-in Steam Workshop DB: status is offline, refresh is explicit."""

    _SAMPLE = {
        "version": 1790337009,
        "database": {
            "2009463077": {
                "packageId": "brrainz.harmony",
                "name": "Harmony",
                "gameVersions": ["1.5", "1.6"],
            },
            "111": {"packageId": "a.mod", "name": "A", "gameVersions": ["1.6"]},
        },
    }

    class _Resp:
        status = 200
        headers = {"ETag": '"api"'}

        def __init__(self, payload: bytes):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self._payload

    @classmethod
    def _cache_path(cls) -> Path:
        return Path.home() / ".rwmod" / "steam_db.json"

    @classmethod
    def _write_cache(cls) -> None:
        cls._cache_path().parent.mkdir(parents=True, exist_ok=True)
        cls._cache_path().write_text(json.dumps(cls._SAMPLE), encoding="utf-8")

    def _patch_download(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        import rwmod.steam_db as sd_mod

        calls: list[int] = []

        def fake(req, timeout=30):
            calls.append(1)
            return self._Resp(json.dumps(self._SAMPLE).encode())

        monkeypatch.setattr(sd_mod, "urlopen", fake)
        return calls

    def test_status_does_not_download(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        import rwmod.steam_db as sd_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        body = _assert_200_data(client.get("/api/rimsort/steamdb/status"))

        assert body["cached"] is False
        assert body["valid"] is False
        assert body["entries"] == 0
        assert body["enabled"] is False
        assert not self._cache_path().exists()

    def test_status_reports_cached_entries(self, client: TestClient):
        self._write_cache()
        body = _assert_200_data(client.get("/api/rimsort/steamdb/status"))

        assert body["cached"] is True
        assert body["valid"] is True
        assert body["entries"] == 2
        assert body["version"] == 1790337009
        assert body["updated_at"] is not None
        assert body["size"] > 0

    def test_refresh_downloads_then_status_sees_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        calls = self._patch_download(monkeypatch)

        body = _assert_200_data(client.post("/api/rimsort/steamdb/refresh"))

        assert calls == [1]
        assert body["ok"] is True
        assert body["cached"] is True
        assert body["entries"] == 2
        status = _assert_200_data(client.get("/api/rimsort/steamdb/status"))
        assert status["cached"] is True
        assert status["entries"] == 2

    def test_refresh_failure_uses_error_contract(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.steam_db as sd_mod

        def boom(req, timeout=30):
            raise OSError("offline")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        _assert_error(client.post("/api/rimsort/steamdb/refresh"), 502)

    def test_refresh_honors_config_size_limit(
        self, client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch
    ):
        self._patch_download(monkeypatch)
        cfg.steam_db_max_bytes = 32
        cfg.save()

        _assert_error(client.post("/api/rimsort/steamdb/refresh"), 502)
        assert not self._cache_path().exists()

    def test_check_order_uses_cache_only_when_enabled(
        self, client: TestClient, cfg, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.steam_db as sd_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(sd_mod, "urlopen", boom)
        self._write_cache()
        _make_mod(cfg.mods_dir, "Aaa", package_id="a.mod")
        target = cfg.rimworld_dir / "ModsConfig.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "<ModsConfigData><version>1.6.4850 rev645</version><activeMods>"
            "<li>brrainz.harmony</li><li>a.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        disabled = _assert_200_data(client.get("/api/rimsort/check-order"))
        assert "steam_db" not in disabled

        cfg.enable_steam_db = True
        cfg.save()
        enabled = _assert_200_data(client.get("/api/rimsort/check-order"))
        assert enabled["steam_db"]["game_versions"]["a.mod"] == ["1.6"]


class TestConfigDirWiring:
    """HTTP requests must carry ``Config.config_dir`` to every ModsConfig writer.

    The focused sandbox regressions live in tests/test_config_path_safety.py;
    these drive the same paths through ``Depends(get_config)`` so a broken
    config_dir round-trip cannot hide behind direct function calls.
    """

    @staticmethod
    def _use_config_dir(cfg, tmp_path: Path) -> Path:
        from rwmod.config import Config

        config_dir = tmp_path / "explicit_config"
        config_dir.mkdir()
        cfg.config_dir = config_dir
        cfg.save()
        # The request's get_config() must see the persisted value.
        assert Config.load().config_dir == config_dir
        return config_dir / "ModsConfig.xml"

    def test_apply_writes_explicit_config_dir(self, client: TestClient, cfg, tmp_path: Path):
        target = self._use_config_dir(cfg, tmp_path)
        _make_mod(cfg.mods_dir, "Sorted", package_id="pkg.sorted")
        target.write_text(
            "<ModsConfigData><activeMods><li>pkg.sorted</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )

        body = _assert_200_data(client.post("/api/rimsort/apply"))

        assert body["ok"] is True
        assert "<li>pkg.sorted</li>" in target.read_text(encoding="utf-8")
        assert not (cfg.rimworld_dir / "ModsConfig.xml").exists()
        assert list(target.parent.glob("ModsConfig.xml.bak_*"))

    def test_profiles_save_and_restore_use_explicit_config_dir(
        self, client: TestClient, cfg, tmp_path: Path
    ):
        target = self._use_config_dir(cfg, tmp_path)
        target.write_text(
            "<ModsConfigData><activeMods><li>pkg.one</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        _assert_200_data(client.post("/api/profiles/save", json={"name": "httpcfg"}))

        target.write_text(
            "<ModsConfigData><activeMods><li>pkg.two</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        body = _assert_200_data(client.post("/api/profiles/httpcfg/restore"))

        assert body["ok"] is True
        assert "pkg.one" in target.read_text(encoding="utf-8")
        assert not (cfg.rimworld_dir / "ModsConfig.xml").exists()

    def test_undo_uses_explicit_config_dir(self, client: TestClient, cfg, tmp_path: Path):
        target = self._use_config_dir(cfg, tmp_path)
        _make_mod(cfg.mods_dir, "Keep", package_id="pkg.keep")
        target.write_text(
            "<ModsConfigData><activeMods><li>pkg.keep</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        _assert_200_data(client.post("/api/rimsort/apply"))

        # A later edit that the pre-apply snapshot must roll back.
        target.write_text(
            "<ModsConfigData><activeMods><li>junk.mod</li></activeMods></ModsConfigData>",
            encoding="utf-8",
        )
        body = _assert_200_data(client.post("/api/undo"))

        assert body["ok"] is True
        restored = target.read_text(encoding="utf-8")
        assert "pkg.keep" in restored
        assert "junk.mod" not in restored
        assert not (cfg.rimworld_dir / "ModsConfig.xml").exists()


class TestWorkshopRouterBranches:
    def test_search_marks_installed_mods(self, client: TestClient, cfg):
        from rwmod.workshop import ModSearchResult

        _make_mod(cfg.mods_dir, "Inst", package_id="i.n", workshop_id="777")
        hit = ModSearchResult(id="777", title="T", author="A")
        with patch("rwmod.routers.workshop.search_workshop", return_value=[hit]):
            body = _assert_200_data(client.get("/api/search?q=harmony"))
        assert body["results"][0]["installed"] is True
        assert set(body["results"][0]) == {
            "id",
            "title",
            "author",
            "description",
            "preview_url",
            "rating",
            "subscribers",
            "installed",
        }

    def test_collection_preview_splits_buckets(self, client: TestClient, cfg):
        from rwmod.database import record_download

        _make_mod(cfg.mods_dir, "Have", workshop_id="111")
        record_download("222", "failed")
        with patch(
            "rwmod.routers.workshop.fetch_collection_children",
            return_value=["111", "222", "333"],
        ):
            body = _assert_200_data(client.get("/api/collection/preview/123456"))
        assert body["installed_count"] == 1
        assert body["failed_before"] == ["222"]
        assert body["new_mods"] == ["333"]
        assert body["total"] == 3

    def test_collection_import_accepts_a_url_id(self, client: TestClient):
        """A workshop URL in the request body resolves to its numeric id."""
        with (
            patch("rwmod.routers.download.fetch_collection_children", return_value=["1"]) as fetch,
            patch(
                "rwmod.routers.download.download_batch",
                side_effect=lambda _cfg, ids, **k: {i: True for i in ids},
            ),
        ):
            _assert_200_data(
                client.post(
                    "/api/import/collection",
                    json={
                        "collection_id": "https://steamcommunity.com/sharedfiles/"
                        "filedetails/?id=999888"
                    },
                )
            )
        fetch.assert_called_once_with("999888")


class TestMiscContractBranches:
    def test_transfer_export_is_a_binary_attachment(self, client: TestClient):
        resp = client.post("/api/transfer/export", json={"include_backups": False})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert resp.content[:2] == b"PK"
        assert "X-Export-Summary" in resp.headers

    def test_metrics_is_prometheus_text(self, client: TestClient):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "# TYPE rwmod_uptime_seconds gauge" in resp.text
        assert "rwmod_requests_total{" in resp.text

    def test_metrics_escapes_path_label(self, client: TestClient):
        client.get("/api/tags/by-tag/a%22b")
        text = client.get("/metrics").text
        offending = [ln for ln in text.splitlines() if "by-tag/a" in ln and ln.count('"') != 2]
        assert not offending, f"unescaped quote in metrics label: {offending}"
        # Path parameters are collapsed to the route template, so the label
        # is a bounded set rather than one series per arbitrary tag value.
        assert 'path="/api/tags/by-tag/{tag}"' in text

    def test_metrics_escapes_label_on_unmatched_path(self, client: TestClient):
        """An unmatched path has no route template, so the raw (attacker
        controlled) spelling is used and must still be escaped."""
        client.get("/no-such-route/a%22b%5Cc")
        text = client.get("/metrics").text
        line = next(ln for ln in text.splitlines() if "no-such-route" in ln)
        assert 'path="/no-such-route/a\\"b\\\\c"' in line, line

    def test_metrics_series_cardinality_is_bounded(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        from collections import defaultdict

        from rwmod.routers import metrics as metrics_mod

        fresh: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_ms": 0.0, "errors": 0})
        monkeypatch.setattr(metrics_mod, "_request_stats", fresh)
        for i in range(metrics_mod.MAX_METRIC_SERIES + 25):
            client.get(f"/definitely-not-a-route/{i}")
        # Every 404 path would be its own series; the cap collapses the tail
        # into one bucket instead of growing memory without bound.
        assert len(fresh) <= metrics_mod.MAX_METRIC_SERIES + 1
        assert "GET __other__" in fresh

    def test_root_serves_html_or_explains_missing_build(self, client: TestClient):
        resp = client.get("/")
        assert resp.status_code in (200, 503), resp.text
        if resp.status_code == 503:
            # static/ is a build artifact; the documented answer is an actionable 503.
            _assert_error(resp, 503)
        else:
            assert resp.headers["content-type"].startswith("text/html")


class TestUploadFailureContract:
    """Hostile-but-well-formed uploads must fail as 4xx {error, detail}."""

    @staticmethod
    def _zip(members: list[str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name in members:
                zf.writestr(name, "<ModMetaData/>")
        return buf.getvalue()

    def test_import_local_traversal_zip_is_4xx(self, client: TestClient):
        resp = client.post(
            "/api/mods/import-local",
            files={"file": ("m.zip", self._zip(["../evil.txt"]), "application/zip")},
        )
        assert resp.status_code < 500, resp.text
        _assert_error(resp, resp.status_code)

    def test_import_local_corrupt_zip_is_4xx(self, client: TestClient):
        resp = client.post(
            "/api/mods/import-local",
            files={"file": ("m.zip", b"PK\x03\x04" + b"garbage" * 8, "application/zip")},
        )
        assert resp.status_code < 500, resp.text
        _assert_error(resp, resp.status_code)

    def test_transfer_import_traversal_zip_is_400(self, client: TestClient):
        """The transfer router does catch BadZipFile — same input, good shape."""
        resp = client.post(
            "/api/transfer/import",
            files={"file": ("b.rwmod", self._zip(["../evil.txt"]), "application/zip")},
        )
        _assert_error(resp, 400)


class TestShutdownContract:
    """The lifespan shutdown path is part of the HTTP server contract: once the
    queue worker has been started, *any* request can have started one."""

    def test_stop_then_wait_stopped_returns_quietly(self, cfg):
        import asyncio

        from rwmod.queue import DownloadQueue

        async def scenario() -> None:
            queue = DownloadQueue()
            await queue.start(cfg)
            await asyncio.sleep(0)  # let the worker task actually begin
            queue.stop()
            await queue.wait_stopped(timeout=1.0)

        asyncio.run(scenario())


class TestQueueControlEndpoints:
    """P2-1: pause/resume state and one-click retry of failed items."""

    def test_pause_resume_reflected_in_state(self, client: TestClient):
        assert _assert_200_data(client.post("/api/queue/pause")) == {"paused": True}
        state = _assert_200_data(client.get("/api/queue"))
        assert state["paused"] is True

        assert _assert_200_data(client.post("/api/queue/resume")) == {"paused": False}
        assert _assert_200_data(client.get("/api/queue"))["paused"] is False

    def test_retry_with_no_failed_items_returns_zero(self, client: TestClient):
        body = _assert_200_data(client.post("/api/queue/retry", json={"ids": []}))
        assert body == {"accepted": 0}

    def test_retry_resets_failed_item_and_starts_worker(self, client: TestClient):
        from unittest.mock import AsyncMock

        from rwmod.queue import get_queue
        from rwmod.steamcmd import ErrorKind

        q = get_queue()
        q.add(["111"])
        item = q.items[0]
        item.status = "failed"
        item.retry_count = 1
        item.error_kind = ErrorKind.UNKNOWN
        item.error_detail = "boom"

        with patch.object(q, "start", new=AsyncMock()) as start:
            body = _assert_200_data(client.post("/api/queue/retry", json={"ids": ["111"]}))

        assert body == {"accepted": 1}
        assert item.status == "pending"
        assert item.retry_count == 0
        assert item.error_kind == ""
        start.assert_awaited_once()
