"""Tests for skymods.py — URL extraction, zip handling, no network required.

All network calls are mocked; real extraction is exercised against temp dirs.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from rwmod.config import Config
from rwmod.skymods import (
    _download_and_extract,
    _extract_download_url,
    _find_mod_folder,
    try_skymods,
)


class TestExtractDownloadUrl:
    def test_skymods_excerpt_btn(self):
        html = '<a class="skymods-excerpt-btn" href="https://modsbase.com/abc">Download</a>'
        assert _extract_download_url(html, "123") == "https://modsbase.com/abc"

    def test_modsbase_link(self):
        html = '<a href="https://modsbase.com/xyz">Download</a>'
        assert _extract_download_url(html, "456") == "https://modsbase.com/xyz"

    def test_no_match(self):
        assert _extract_download_url("<html></html>", "123") is None


class TestFindModFolder:
    def test_root_about(self, tmp_path: Path):
        (tmp_path / "About").mkdir()
        (tmp_path / "About" / "About.xml").touch()
        assert _find_mod_folder(tmp_path) == tmp_path

    def test_one_level_deep(self, tmp_path: Path):
        inner = tmp_path / "inner"
        (inner / "About").mkdir(parents=True)
        (inner / "About" / "About.xml").touch()
        assert _find_mod_folder(tmp_path) == inner

    def test_two_levels_deep(self, tmp_path: Path):
        inner = tmp_path / "a" / "b"
        (inner / "About").mkdir(parents=True)
        (inner / "About" / "About.xml").touch()
        assert _find_mod_folder(tmp_path) == inner

    def test_no_about_falls_back_to_first_dir(self, tmp_path: Path):
        (tmp_path / "somedir").mkdir()
        assert _find_mod_folder(tmp_path) == tmp_path / "somedir"

    def test_empty_dir(self, tmp_path: Path):
        assert _find_mod_folder(tmp_path) is None


class _FakeResp:
    """Fake urllib response: read() advances the position like the real
    ``http.client.HTTPResponse`` (the streaming downloader relies on it)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.headers = {"Content-Type": "application/zip"}

    def read(self, size: int = -1) -> bytes:
        """Read up to `size` bytes from the current position."""
        if size is None or size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def _make_zip_bytes(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _patch_opener(data: bytes) -> MagicMock:
    """Return a fake build_opener result that yields _FakeResp(data)."""
    opener = MagicMock()
    opener.open.return_value = MagicMock()
    opener.open.return_value.__enter__.return_value = _FakeResp(data)
    return opener


class TestDownloadAndExtract:
    def test_success(self, tmp_path: Path):
        data = _make_zip_bytes(
            {
                "About/About.xml": "<ModMetaData><packageId>pkg.test</packageId></ModMetaData>",
                "About/PublishedFileId.txt": "123",
                "Textures/pad.txt": "x" * 4096,  # push total size over the 512B sniff floor
            }
        )
        cfg = Config(mods_dir=tmp_path / "Mods")
        (tmp_path / "Mods").mkdir()
        opener = _patch_opener(data)
        with (
            patch("rwmod.skymods._shared_opener", opener),
            patch("rwmod.skymods._is_safe_download_url", return_value=True),
        ):
            dest = _download_and_extract("https://example.com/x.zip", "123", cfg)
        assert dest is not None
        assert dest.exists()
        assert (dest / "About" / "About.xml").exists()

    def test_html_response_returns_none(self, tmp_path: Path):
        cfg = Config(mods_dir=tmp_path / "Mods")
        (tmp_path / "Mods").mkdir()
        opener = _patch_opener(b"<!DOCTYPE html><html></html>")
        with (
            patch("rwmod.skymods._shared_opener", opener),
            patch("rwmod.skymods._is_safe_download_url", return_value=True),
        ):
            dest = _download_and_extract("https://example.com/x", "123", cfg)
        assert dest is None

    def test_not_a_zip_returns_none(self, tmp_path: Path):
        cfg = Config(mods_dir=tmp_path / "Mods")
        (tmp_path / "Mods").mkdir()
        with patch(
            "rwmod.skymods.urllib.request.build_opener",
            return_value=_patch_opener(b"PK\x03\x04not really a zip"),
        ):
            dest = _download_and_extract("https://example.com/x.zip", "123", cfg)
        assert dest is None

    def test_rejects_path_traversal(self, tmp_path: Path):
        data = _make_zip_bytes({"../evil.txt": "boom"})
        cfg = Config(mods_dir=tmp_path / "Mods")
        (tmp_path / "Mods").mkdir()
        with patch("rwmod.skymods.urllib.request.build_opener", return_value=_patch_opener(data)):
            dest = _download_and_extract("https://example.com/x.zip", "123", cfg)
        assert dest is None
        assert not (tmp_path / "evil.txt").exists()


class TestTrySkymods:
    def test_no_download_url_returns_none(self, tmp_path: Path):
        resp = MagicMock()
        resp.read.return_value = b"<html></html>"
        urlopen = MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=resp)))
        cfg = Config(mods_dir=tmp_path / "Mods")
        with patch("rwmod.skymods.urllib.request.urlopen", urlopen):
            assert try_skymods("123", cfg) is None

    def test_success(self, tmp_path: Path):
        resp = MagicMock()
        resp.read.return_value = (
            b'<a class="skymods-excerpt-btn" href="https://modsbase.com/dl">Download</a>'
        )
        urlopen = MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=resp)))
        cfg = Config(mods_dir=tmp_path / "Mods")
        dest = tmp_path / "Mods" / "T"
        with (
            patch("rwmod.skymods.urllib.request.urlopen", urlopen),
            patch("rwmod.skymods._download_and_extract", return_value=dest),
        ):
            assert try_skymods("123", cfg) == dest

    def test_network_error_returns_none(self, tmp_path: Path):
        from urllib.error import URLError

        urlopen = MagicMock(side_effect=URLError("no network"))
        cfg = Config(mods_dir=tmp_path / "Mods")
        with patch("rwmod.skymods.urllib.request.urlopen", urlopen):
            assert try_skymods("123", cfg) is None
