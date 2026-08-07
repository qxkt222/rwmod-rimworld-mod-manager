"""Batch-1 security fixes — upload caps, config path validation, secret redaction, WS subprotocol auth.

Covers:
- read_upload_limited() enforces the cap even when the client sends no
  Content-Length (UploadFile.size is None) — the old ``if file.size`` guard
  was silently skipped for chunked uploads.
- transfer._save_upload() streams to disk with a hard cap.
- .rwmod export redacts the Steam API key by default.
- /ws accepts the JWT via Sec-WebSocket-Protocol instead of only ?token=.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# ── upload cap helpers ─────────────────────────────────────────────


class FakeAsyncFile:
    """Minimal async file-like with a fixed body (no Content-Length concept)."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if n < 0 or n >= len(self._data):
            return self._data
        return self._data[:n]


class TestReadUploadLimited:
    def test_within_limit(self):
        from rwmod.utils import read_upload_limited

        body = b"x" * 100
        import asyncio

        out = asyncio.run(read_upload_limited(FakeAsyncFile(body), max_bytes=200))
        assert out == body

    def test_exact_limit(self):
        import asyncio

        from rwmod.utils import read_upload_limited

        out = asyncio.run(read_upload_limited(FakeAsyncFile(b"x" * 200), max_bytes=200))
        assert out == b"x" * 200

    def test_over_limit_returns_none(self):
        """The old guard (``file.size is not None``) was bypassed when the
        client sent no Content-Length; the hard read-cap must catch it."""
        import asyncio

        from rwmod.utils import read_upload_limited

        out = asyncio.run(read_upload_limited(FakeAsyncFile(b"x" * 201), max_bytes=200))
        assert out is None


class TestTransferSaveUpload:
    def _make_upload(self, data: bytes):
        from starlette.datastructures import UploadFile

        class _File:
            """Seekable-ish in-memory reader: read() advances the position,
            like the SpooledTemporaryFile behind a real UploadFile."""

            def __init__(self, data: bytes):
                self._data = data
                self._pos = 0

            def read(self, n: int = -1) -> bytes:
                if n < 0:
                    n = len(self._data) - self._pos
                chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
                return chunk

        return UploadFile(file=_File(data))

    def test_streams_within_limit(self, tmp_path: Path):
        from rwmod.routers.transfer import _save_upload

        out = tmp_path / "bundle.rwmod"
        _save_upload(self._make_upload(b"y" * 5000), out, max_bytes=10000)
        assert out.read_bytes() == b"y" * 5000

    def test_over_limit_raises_413(self, tmp_path: Path):
        from fastapi import HTTPException

        from rwmod.routers.transfer import _save_upload

        with pytest.raises(HTTPException) as exc:
            _save_upload(self._make_upload(b"y" * 5000), tmp_path / "bundle.rwmod", max_bytes=1000)
        assert exc.value.status_code == 413


# ── config path validation (API level) ─────────────────────────────


class TestConfigPathValidation:
    def test_relative_path_rejected(self, client: TestClient):
        resp = client.post("/api/config", json={"mods_dir": "relative/path"})
        assert resp.status_code == 400

    def test_system_root_rejected(self, client: TestClient):
        resp = client.post("/api/config", json={"mods_dir": "C:\\"})
        assert resp.status_code == 400

    def test_steamcmd_dir_rejected(self, client: TestClient, tmp_path):
        resp = client.post("/api/config", json={"steamcmd_path": str(tmp_path)})
        assert resp.status_code == 400

    def test_valid_absolute_path_accepted(self, client: TestClient, tmp_path):
        resp = client.post("/api/config", json={"mods_dir": str(tmp_path / "Mods2")})
        assert resp.status_code == 200


# ── .rwmod secret redaction ─────────────────────────────────────────


class TestTransferSecrets:
    def _cfg_with_key(self, tmp_path: Path):
        from rwmod.config import Config

        cfg = Config(steamcmd_path=tmp_path / "steamcmd.exe", mods_dir=tmp_path / "Mods")
        cfg.steam_api_key = "sk_live_secret_123"
        return cfg

    def test_export_config_redacts_by_default(self, tmp_path: Path):
        from rwmod.transfer import _export_config

        cfg = self._cfg_with_key(tmp_path)
        assert "sk_live_secret_123" not in _export_config(cfg)
        assert "sk_live_secret_123" in _export_config(cfg, include_secrets=True)

    def test_export_bundle_manifest_marks_key_present(self, tmp_path: Path):
        import json
        import zipfile

        from rwmod.transfer import export_bundle

        cfg = self._cfg_with_key(tmp_path)
        out = tmp_path / "bundle.rwmod"
        export_bundle(cfg, out, include_backups=False)
        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["has_steam_api_key"] is True
        # And the config.toml inside must NOT contain the key.
        with zipfile.ZipFile(out) as zf:
            config_toml = zf.read("config.toml").decode()
        assert "sk_live_secret_123" not in config_toml

    def test_export_bundle_with_secrets_contains_key(self, tmp_path: Path):
        import zipfile

        from rwmod.transfer import export_bundle

        cfg = self._cfg_with_key(tmp_path)
        out = tmp_path / "bundle.rwmod"
        export_bundle(cfg, out, include_backups=False, include_secrets=True)
        with zipfile.ZipFile(out) as zf:
            config_toml = zf.read("config.toml").decode()
        assert "sk_live_secret_123" in config_toml


# ── WebSocket subprotocol auth ──────────────────────────────────────


class TestWebSocketAuth:
    def test_subprotocol_token_accepted(self, client: TestClient):
        from rwmod.auth import create_token

        token = create_token("admin")
        with client.websocket_connect("/ws", subprotocols=[f"rwmod.{token}"]) as ws:
            ws.send_json({"cmd": "ping"})
            msg = ws.receive_json()
            assert msg["type"] == "pong"

    def test_no_token_rejected(self, client: TestClient):
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws"):
                pass  # must be closed with 1008 before accept

    def test_bad_token_rejected(self, client: TestClient):
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws", subprotocols=["rwmod.invalid"]):
                pass
