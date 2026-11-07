"""SEC-* adversarial probes — try to *bypass* the documented safety guards.

Complements tests/test_security_batch.py (upload cap / config path basics /
secret redaction) and tests/test_skymods.py (URL extraction, happy path) by
attacking the guards themselves:

- ``safe_extract_zip``: traversal spellings, duplicate spellings, thresholds
  exactly on the boundary, lying size headers, symlink members.
- ``read_upload_limited``: boundary bytes and the "never buffer more than
  cap+1" contract.
- ``extract_mod_id`` / SteamCMD: numeric-validation bypass attempts feeding a
  command line.
- config path blocklist: Windows path aliases (``\\\\?\\``, device, UNC).
- backup / .rwmod import: traversal through filenames and through a crafted
  bundle (the SECURITY.md threat model).
- Skymods SSRF: host allowlist, private-address rejection, redirect hops,
  response caps.
- secrets: ``/api/config`` must never echo the Steam API key; security
  headers / CORS must stay tight.

Everything is offline: SSRF probes either monkeypatch DNS or talk to a
loopback HTTP server. No external request is ever made.
"""

from __future__ import annotations

import gzip
import io
import os
import socket
import struct
import threading
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from rwmod.backup import _resolve_backup_path, backup_mod, restore_mod
from rwmod.config import Config
from rwmod.routers.config import _validate_dir_path
from rwmod.skymods import (
    _download_and_extract,
    _DownloadTooBig,
    _host_allowed,
    _is_safe_download_url,
    _stream_to_file,
)
from rwmod.steamcmd import SteamCMD
from rwmod.transfer import import_bundle
from rwmod.utils import extract_mod_id, read_upload_limited, safe_extract_zip

# ── helpers ────────────────────────────────────────────────────────


def _zip_with(path: Path, members: list[tuple[str, bytes]]) -> Path:
    """Write a zip with duplicate names allowed (dicts cannot hold them)."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


def _all_files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


def _extract(dest: Path, members: list[tuple[str, bytes]], **kwargs: Any) -> None:
    with zipfile.ZipFile(_zip_with(dest.parent / "_probe.zip", members)) as zf:
        safe_extract_zip(zf, dest, **kwargs)


class _FakeResp:
    """Response double whose read() advances like http.client.HTTPResponse."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.headers = {"Content-Type": "application/zip"}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


class _FakeCtx:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def __enter__(self) -> _FakeResp:
        return self._resp

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    """Opener double: records request URLs, serves queued responses.

    Raises AssertionError for any request beyond the queued responses, so a
    test that expects "no outbound request" fails loudly if one is made.
    """

    def __init__(self, *responses: _FakeResp) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def open(self, req: Any, timeout: float | None = None) -> _FakeCtx:
        self.urls.append(req.full_url)
        if not self._responses:
            raise AssertionError(f"unexpected outbound request: {req.full_url}")
        return _FakeCtx(self._responses.pop(0))


# ── safe_extract_zip: traversal spellings ──────────────────────────

# Path semantics are platform-specific: ':'/drive letters are rejected as
# unsafe only on Windows, and '\' is a separator only on Windows. Windows-only
# cases are marked so Windows coverage stays intact, while POSIX gets an
# explicit confinement assertion instead of a false rejection expectation.
_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX filename semantics")

_TRAVERSAL_MEMBERS = [
    "../evil.txt",
    "..\\evil.txt",
    "a/../../evil.txt",
    "a\\..\\..\\evil.txt",
    "/abs.txt",
    "\\abs.txt",
    "//srv/share/evil.txt",
    "\\\\srv\\share\\evil.txt",
    "./../evil.txt",
    ".\\..\\evil.txt",
    "a/./../../evil.txt",
    "....//evil.txt",
    "..../evil.txt",
    ".../evil.txt",  # trailing-dot component: Windows folds "..." to ".."
    ".. /evil.txt",  # trailing-space component
    "sub/../../evil.txt",
    "a\\..\\..\\..\\evil.txt",
    "..\\..\\evil.txt ",
    "\\\\?\\D:\\evil.txt",
    "\\\\.\\NUL",
]

# ':' and drive letters are ordinary filename characters on POSIX, so these
# spellings are not traversal there — only Windows rejects them as unsafe.
_WINDOWS_TRAVERSAL_MEMBERS = [
    "C:/evil.txt",
    "C:\\evil.txt",
    "c:/windows/evil.txt",
    "::",
    "a:b/evil.txt",
]

_TRAVERSAL_MEMBER_PARAMS = [
    *_TRAVERSAL_MEMBERS,
    *[pytest.param(member, marks=_WINDOWS_ONLY) for member in _WINDOWS_TRAVERSAL_MEMBERS],
]


class TestSafeExtractZipTraversal:
    @pytest.mark.parametrize("member", _TRAVERSAL_MEMBER_PARAMS)
    def test_traversal_member_rejected_and_confined(self, tmp_path: Path, member: str) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        z = _zip_with(tmp_path / "_probe.zip", [(member, b"pwn")])
        before = _all_files(tmp_path)
        with pytest.raises(zipfile.BadZipFile):
            with zipfile.ZipFile(z) as zf:
                safe_extract_zip(zf, dest)
        # Nothing may be created outside dest_dir (dest itself stays empty too:
        # the whole archive is validated before extractall runs).
        assert _all_files(tmp_path) - before == set()
        assert _all_files(dest) == set()

    @_POSIX_ONLY
    @pytest.mark.parametrize("member", _WINDOWS_TRAVERSAL_MEMBERS)
    def test_colon_members_confined_on_posix(self, tmp_path: Path, member: str) -> None:
        """The Windows-only spellings above are plain names on POSIX:
        extraction may succeed or be rejected, but the result must never land
        outside dest."""
        dest = tmp_path / "dest"
        dest.mkdir()
        z = _zip_with(tmp_path / "_probe.zip", [(member, b"pwn")])
        before = _all_files(tmp_path)
        try:
            with zipfile.ZipFile(z) as zf:
                safe_extract_zip(zf, dest)
        except zipfile.BadZipFile:
            pass
        outside = _all_files(tmp_path) - _all_files(dest) - before
        assert outside == set()

    @pytest.mark.parametrize(
        "member",
        ["..%2f..%2fevil.txt", "..%5c..%5cevil.txt", "..\u00a0/evil.txt", "\uff0e\uff0e/e.txt"],
    )
    def test_lookalikes_do_not_escape(self, tmp_path: Path, member: str) -> None:
        """URL-encoding, Unicode lookalikes and drive-relative names are not
        traversal — they must land as literal names *inside* dest (or be
        rejected), never outside."""
        dest = tmp_path / "dest"
        dest.mkdir()
        z = _zip_with(tmp_path / "_probe.zip", [(member, b"pwn")])
        try:
            with zipfile.ZipFile(z) as zf:
                safe_extract_zip(zf, dest)
        except zipfile.BadZipFile:
            pass
        outside = _all_files(tmp_path) - _all_files(dest) - {tmp_path / "_probe.zip"}
        assert outside == set()

    def test_drive_relative_member_confined(self, tmp_path: Path) -> None:
        """``C:evil.txt`` is drive-relative: on a C: dest it folds into dest
        (zipfile strips the drive), on another drive resolve() rejects it.
        Confinement is the property that must hold either way."""
        dest = tmp_path / "dest"
        dest.mkdir()
        z = _zip_with(tmp_path / "_probe.zip", [("C:evil.txt", b"pwn")])
        try:
            with zipfile.ZipFile(z) as zf:
                safe_extract_zip(zf, dest)
        except zipfile.BadZipFile:
            pass
        outside = _all_files(tmp_path) - _all_files(dest) - {tmp_path / "_probe.zip"}
        assert outside == set()

    def test_trailing_space_member_raises_documented_error(self, tmp_path: Path) -> None:
        """A member named ``".. "`` sails through every check in
        safe_extract_zip (it is not ``..``, has no ``/../`` and resolves to
        dest itself) and then blows up inside zipfile.extractall with
        ValueError('Empty filename.') — an exception type outside the
        documented ``zipfile.BadZipFile`` contract and outside every caller's
        ``except (BadZipFile, OSError)``."""
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(zipfile.BadZipFile):
            _extract(dest, [(".. ", b"pwn")])

    def test_symlink_member_is_not_followed(self, tmp_path: Path) -> None:
        """A symlink-flagged member must not turn into a real link on disk
        (nothing may be written through it)."""
        dest = tmp_path / "dest"
        dest.mkdir()
        outside = tmp_path / "outside"
        z = tmp_path / "sym.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zi = zipfile.ZipInfo("link")
            zi.external_attr = (0o120777) << 16  # S_IFLNK
            zf.writestr(zi, str(outside))
            zf.writestr("link/evil.txt", "pwn")
        try:
            with zipfile.ZipFile(z) as zf:
                safe_extract_zip(zf, dest)
        except (zipfile.BadZipFile, OSError):
            pass
        assert not (dest / "link").is_symlink()
        assert not outside.exists()


class TestSafeExtractZipLimits:
    def test_member_count_boundary(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        members = [(f"f{i}.txt", b"x") for i in range(3)]
        # Exactly at the cap is accepted; one over is rejected.
        _extract(dest, members, max_members=3)
        assert len(_all_files(dest)) == 3
        with pytest.raises(zipfile.BadZipFile):
            _extract(dest, members, max_members=2)

    def test_total_size_boundary(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        members = [("a.txt", b"x" * 100), ("b.txt", b"y" * 100)]
        _extract(dest, members, max_total_bytes=200)
        with pytest.raises(zipfile.BadZipFile):
            _extract(dest, members, max_total_bytes=199)

    def test_bomb_rejected_before_any_extraction(self, tmp_path: Path) -> None:
        """The docstring promises bomb checks run BEFORE extracting: a zip
        with one legitimate member first must still leave dest empty."""
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(zipfile.BadZipFile):
            _extract(dest, [("ok.txt", b"fine"), ("bomb.bin", b"x" * 1000)], max_total_bytes=10)
        assert _all_files(dest) == set()

    def test_understated_size_header_cannot_write_more(self, tmp_path: Path) -> None:
        """A zip that lies about its uncompressed size (declares 0, carries
        4 MB) must not write the payload — zipfile stops at the declared
        size, so summing ``file_size`` really is a bound on bytes written."""
        member = "big.bin"
        payload = b"A" * (4 * 1024 * 1024)
        plain = tmp_path / "plain.zip"
        with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(member, payload)
        with zipfile.ZipFile(plain) as zf:
            info = zf.infolist()[0]
            cd = zf.start_dir
            data = bytearray(plain.read_bytes())
        off = info.header_offset
        assert data[off : off + 4] == b"PK\x03\x04"
        struct.pack_into("<I", data, off + 18, 0)  # local csize
        struct.pack_into("<I", data, off + 22, 0)  # local usize
        struct.pack_into("<I", data, cd + 20, 0)  # central csize
        struct.pack_into("<I", data, cd + 24, 0)  # central usize
        liar = tmp_path / "liar.zip"
        liar.write_bytes(bytes(data))

        dest = tmp_path / "dest"
        dest.mkdir()
        try:
            with zipfile.ZipFile(liar) as zf:
                safe_extract_zip(zf, dest, max_total_bytes=10, max_members=10)
        except zipfile.BadZipFile:
            pass
        written = dest / member
        assert not written.exists() or written.stat().st_size == 0

    def test_duplicate_exact_name_rejected(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(zipfile.BadZipFile):
            _extract(dest, [("a.txt", b"1"), ("a.txt", b"2")])

    def test_duplicate_via_backslash_normalization_rejected(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        with pytest.raises(zipfile.BadZipFile):
            _extract(dest, [("a/b.txt", b"1"), ("a\\b.txt", b"2")])

    def test_dot_prefixed_duplicate_not_detected_but_confined(self, tmp_path: Path) -> None:
        """``./a.txt`` and ``a.txt`` are the same file after normalization but
        only the raw string is compared, so the duplicate guard misses it.
        Impact is limited to a within-dest overwrite (recorded as a hardening
        note, not a defect) — assert at least that nothing escapes."""
        dest = tmp_path / "dest"
        dest.mkdir()
        _extract(dest, [("a.txt", b"1"), ("./a.txt", b"2")])
        assert (dest / "a.txt").read_bytes() == b"2"
        outside = _all_files(tmp_path) - _all_files(dest) - {tmp_path / "_probe.zip"}
        assert outside == set()


# ── read_upload_limited ────────────────────────────────────────────


class _FakeAsyncFile:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.read_sizes: list[int] = []

    async def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        if n < 0:
            return self._data
        return self._data[:n]


class TestReadUploadLimited:
    def test_exact_cap_ok_and_one_over_rejected(self) -> None:
        import asyncio

        assert asyncio.run(read_upload_limited(_FakeAsyncFile(b"x" * 100), 100)) == b"x" * 100
        assert asyncio.run(read_upload_limited(_FakeAsyncFile(b"x" * 101), 100)) is None

    def test_zero_cap_edge(self) -> None:
        import asyncio

        assert asyncio.run(read_upload_limited(_FakeAsyncFile(b""), 0)) == b""
        assert asyncio.run(read_upload_limited(_FakeAsyncFile(b"x"), 0)) is None

    def test_never_asks_for_more_than_cap_plus_one(self) -> None:
        """The whole point of the helper: the read request itself is capped,
        so a chunked body is never buffered beyond cap+1 bytes."""
        import asyncio

        f = _FakeAsyncFile(b"x" * 10_000)
        assert asyncio.run(read_upload_limited(f, 256)) is None
        assert f.read_sizes == [257]


# ── extract_mod_id ─────────────────────────────────────────────────


class TestExtractModIdRejectsNonNumeric:
    @pytest.mark.parametrize(
        "raw",
        [
            "1e5",
            "0x10",
            "-123",
            "+123",
            "12 34",
            "123.0",
            "id=",
            "?id=",
            "",
            "   ",
            "abc",
            "1;2",
            "$(id)",
            "12\n34",
            "1\x0023",
            "?%69d=123",  # percent-encoded 'id' is not decoded by the regex
            "#id=123",  # fragment marker is not a query separator
        ],
    )
    def test_rejected(self, raw: str) -> None:
        assert extract_mod_id(raw) == ""

    @pytest.mark.parametrize(
        "raw",
        [
            "2009463077",
            " 2009463077 ",
            "?id=2009463077",
            "&id=123",
            "https://steamcommunity.com/sharedfiles/filedetails/?id=99",
            "https://steamcommunity.com/workshop/filedetails/?id=2009463077&foo=bar",
        ],
    )
    def test_accepted(self, raw: str) -> None:
        assert extract_mod_id(raw).isdigit()

    def test_unicode_digits_rejected(self) -> None:
        """``str.isdigit()``/``\\d`` accept non-ASCII digits, so a full-width
        or Arabic-Indic "ID" is reported as a valid numeric workshop id and
        flows into SteamCMD argv and backup filenames.  It cannot inject a
        command token (no spaces/signs), but it does violate the stated
        "workshop IDs are purely numeric" invariant."""
        assert extract_mod_id("\uff11\uff12\uff13") == ""
        assert extract_mod_id("\u0661\u0662\u0663") == ""

    def test_very_long_digit_string_is_not_crashed(self) -> None:
        # Bounded: no regex backtracking blow-up on a huge digit run.
        assert extract_mod_id("9" * 100_000) == "9" * 100_000


# ── config path blocklist ──────────────────────────────────────────


class TestConfigPathBlocklist:
    @pytest.mark.parametrize(
        "raw",
        [
            "C:\\Windows",
            "c:/windows",
            "C:\\Windows ",
            "C:\\Windows.",
            "C:\\Windows\\System32\\..\\..\\Windows",
            "C:\\Program Files (x86)\\Steam",
            "C:\\ProgramData\\rwmod",
            "C:\\Users",
            "C:\\",
        ],
    )
    def test_system_paths_rejected(self, raw: str) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_dir_path(raw, "mods_dir")
        assert exc.value.status_code == 400

    def test_windows_path_aliases_bypass_blocklist(self) -> None:
        """The blocklist compares the *normalized* path string, so Windows
        path aliases that name the same directory (extended-length prefix,
        device path, UNC admin share) skip it entirely and are accepted as
        mods_dir/backup_dir/steamcmd_path."""
        for raw in (
            "\\\\?\\C:\\Windows",
            "\\\\?\\C:\\Windows\\System32",
            "\\\\.\\PhysicalDrive0",
            "\\\\localhost\\C$\\Windows",
        ):
            with pytest.raises(HTTPException):
                _validate_dir_path(raw, "mods_dir")

    def test_trailing_junk_is_not_a_bypass(self) -> None:
        # resolve() strips trailing dots/spaces before the blocklist is applied.
        for raw in ("C:\\Windows ", "C:\\Windows.", "c:\\WINDOWS"):
            with pytest.raises(HTTPException):
                _validate_dir_path(raw, "mods_dir")

    def test_8dot3_short_name_alias_rejected(self) -> None:
        """8.3 aliases are expanded by resolve() before the blocklist runs."""
        alias = Path("C:/PROGRA~1")
        if alias.resolve() == alias:
            pytest.skip("no 8.3 short name for C:\\Program Files on this volume")
        with pytest.raises(HTTPException):
            _validate_dir_path("C:\\PROGRA~1", "mods_dir")


# ── backup / bundle path traversal ─────────────────────────────────


_UNSAFE_BACKUP_NAMES = [
    "../victim.zip",
    "sub/victim.zip",
    "/victim.zip",
    "..",
    ".",
    "",
]

# '\' is a path separator only on Windows; on POSIX these are plain filenames.
_WINDOWS_BACKUP_NAMES = [
    "..\\victim.zip",
    "sub\\victim.zip",
    "C:\\victim.zip",
]

_UNSAFE_BACKUP_NAME_PARAMS = [
    *_UNSAFE_BACKUP_NAMES,
    *[pytest.param(name, marks=_WINDOWS_ONLY) for name in _WINDOWS_BACKUP_NAMES],
]


class TestBackupPathResolution:
    @pytest.mark.parametrize("name", _UNSAFE_BACKUP_NAME_PARAMS)
    def test_unsafe_names_rejected(self, tmp_path: Path, name: str) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        assert _resolve_backup_path(backup_dir, name) is None

    @_POSIX_ONLY
    @pytest.mark.parametrize("name", _WINDOWS_BACKUP_NAMES)
    def test_backslash_names_confined_on_posix(self, tmp_path: Path, name: str) -> None:
        """POSIX treats '\' as an ordinary filename character, so the resolver
        may accept these names — but only directly inside backup_dir."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        got = _resolve_backup_path(backup_dir, name)
        if got is not None:
            # relative_to() raises ValueError if the result escaped backup_dir.
            relative = got.resolve().relative_to(backup_dir.resolve())
            assert relative == Path(name)

    def test_plain_name_accepted(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        got = _resolve_backup_path(backup_dir, "123__mod__20240101_000000.zip")
        assert got == backup_dir / "123__mod__20200101_000000.zip".replace("20200101", "20240101")

    def test_api_delete_cannot_touch_file_outside_backup_dir(self, client: TestClient) -> None:
        cfg = Config.load()
        victim = cfg.backup_dir.parent / "victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        for attack in ("..%2Fvictim.txt", "..%5Cvictim.txt", "%2E%2E%2Fvictim.txt"):
            resp = client.delete(f"/api/backups/{attack}")
            assert resp.status_code in (200, 404, 422)
        assert victim.exists()

    def test_restore_rejects_traversal_filename_via_api(self, client: TestClient) -> None:
        """A traversal filename never resolves to a backup → failure status.

        It used to answer 200 with ``{ok: false}``; the failure contract puts
        the failure in the status code, so this is now a 404.
        """
        resp = client.post(
            "/api/backups/123/restore",
            json={"filename": "..\\..\\evil.zip"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "ModNotFoundError"

    def test_restore_rejects_non_numeric_workshop_id(self, tmp_path: Path) -> None:
        mods = tmp_path / "Mods"
        mods.mkdir()
        backups = tmp_path / "backups"
        backups.mkdir()
        result = restore_mod(mods, "..\\..\\evil", backups)
        assert result["ok"] is False
        assert not (tmp_path / "evil").exists()

    def test_crafted_rwmod_bundle_cannot_escape_mods_dir(self, tmp_path: Path) -> None:
        """SECURITY.md threat model: a crafted .rwmod bundle imports a
        ``backups/`` zip whose *content* traverses; the later restore of that
        backup must still be confined to mods_dir."""
        mods = tmp_path / "Mods"
        mods.mkdir()
        cfg = Config(
            steamcmd_path=tmp_path / "steamcmd.exe",
            mods_dir=mods,
            backup_dir=tmp_path / "backups",
        )
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("../escape.txt", "pwn")
        bundle = tmp_path / "evil.rwmod"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("backups/123__evilmod__20240101_000000.zip", inner.getvalue())

        imported = import_bundle(cfg, bundle)
        assert imported["ok"] is True
        assert (cfg.backup_dir / "123__evilmod__20240101_000000.zip").exists()

        result = restore_mod(cfg.mods_dir, "123", cfg.backup_dir)
        assert result["ok"] is False
        assert not (tmp_path / "escape.txt").exists()
        assert not (mods.parent / "escape.txt").exists()

    def test_bundle_member_traversal_rejected(self, tmp_path: Path) -> None:
        cfg = Config(
            steamcmd_path=tmp_path / "steamcmd.exe",
            mods_dir=tmp_path / "Mods",
            backup_dir=tmp_path / "backups",
        )
        (tmp_path / "Mods").mkdir()
        bundle = tmp_path / "evil.rwmod"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("../escape.txt", "pwn")
        result = import_bundle(cfg, bundle)
        assert result["ok"] is False
        assert not (tmp_path / "escape.txt").exists()

    def test_backup_mod_rejects_non_numeric_id(self, tmp_path: Path) -> None:
        mods = tmp_path / "Mods"
        (mods / "mymod").mkdir(parents=True)
        backups = tmp_path / "backups"
        assert backup_mod(mods, "..\\..\\evil", "mymod", backups) is None
        assert not backups.exists()


# ── SteamCMD command-line injection ────────────────────────────────


class TestSteamcmdNumericValidation:
    def test_injection_shaped_ids_never_spawn(self, tmp_path: Path, monkeypatch) -> None:
        spawned: list[Any] = []
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *a, **kw: spawned.append((a, kw)),  # type: ignore[func-returns-value]
        )
        exe = tmp_path / "steamcmd.exe"
        exe.touch()
        sc = SteamCMD(exe)
        attacks = [
            "+force_install_dir",
            "..\\..\\evil",
            "1;2",
            "12 34",
            "123\n456",
            "`id`",
            "$(id)",
            "-1",
            "1e5",
            "123\x00456",
        ]
        results = sc.workshop_download_many(attacks)
        assert spawned == []
        assert set(results) == set(attacks)
        assert all(not r.success for r in results.values())

    def test_valid_id_reaches_argv_as_single_token(self, tmp_path: Path, monkeypatch) -> None:
        captured: list[list[str]] = []

        class _Proc:
            def __init__(self, cmd: list[str], **kw: Any) -> None:
                captured.append(list(cmd))
                self.stdout = io.StringIO("")
                self.pid = 1

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                pass

        monkeypatch.setattr("subprocess.Popen", _Proc)
        exe = tmp_path / "steamcmd.exe"
        exe.touch()
        SteamCMD(exe).workshop_download_many(["123", "+evil"])
        assert len(captured) == 1
        assert captured[0] == [
            str(exe),
            "+login",
            "anonymous",
            "+workshop_download_item",
            "294100",
            "123",
            "+quit",
        ]


# ── Skymods SSRF ───────────────────────────────────────────────────


class TestSkymodsUrlGuard:
    @pytest.fixture(autouse=True)
    def _public_dns(self, monkeypatch) -> None:
        """Never touch real DNS: allowed hosts resolve to a public address."""
        monkeypatch.setattr(socket, "gethostbyname", lambda host: "93.184.216.34")

    def test_host_allowlist_suffix_tricks(self) -> None:
        assert _host_allowed("smods.ru")
        assert _host_allowed("www.smods.ru")
        assert _host_allowed("cdn.modsbase.com")
        assert not _host_allowed("evilsmods.ru")
        assert not _host_allowed("smods.ru.evil.com")
        assert not _host_allowed("smods.ru.")
        assert not _host_allowed("")

    @pytest.mark.parametrize(
        "url",
        [
            "file:///C:/Windows/win.ini",
            "ftp://smods.ru/x.zip",
            "gopher://smods.ru/x",
            "javascript:alert(1)",
            "http://127.0.0.1/x.zip",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/x.zip",
            "https://user:pass@smods.ru@evil.com/x.zip",
            "https://evil.com#@smods.ru",
            "/relative/x.zip",
            "",
        ],
    )
    def test_dangerous_urls_rejected(self, url: str) -> None:
        assert _is_safe_download_url(url) is False

    def test_allowed_host_is_case_insensitive(self) -> None:
        assert _is_safe_download_url("https://SMODS.RU/x.zip") is True

    def test_allowed_host_resolving_private_rejected(self, monkeypatch) -> None:
        """DNS for an allowlisted host pointing at the LAN/loopback must be
        refused (hosts-file / DNS-rebinding style attacks)."""
        for addr in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "0.0.0.0", "192.168.1.7"):
            monkeypatch.setattr(socket, "gethostbyname", lambda host, a=addr: a)
            assert _is_safe_download_url("https://smods.ru/x.zip") is False

    def test_dns_failure_fails_closed(self, monkeypatch) -> None:
        def boom(host: str) -> str:
            raise OSError("no dns")

        monkeypatch.setattr(socket, "gethostbyname", boom)
        assert _is_safe_download_url("https://smods.ru/x.zip") is False

    def test_non_allowlisted_host_is_not_resolved(self, monkeypatch) -> None:
        calls: list[str] = []

        def spy(host: str) -> str:
            calls.append(host)
            return "93.184.216.34"

        monkeypatch.setattr(socket, "gethostbyname", spy)
        assert _is_safe_download_url("https://evil.com/x.zip") is False
        assert calls == []

    def test_rejected_url_is_never_opened(self, tmp_path: Path, monkeypatch) -> None:
        opener = _FakeOpener()  # any open() raises AssertionError
        monkeypatch.setattr("rwmod.skymods._shared_opener", opener)
        cfg = Config(mods_dir=tmp_path / "Mods")
        (tmp_path / "Mods").mkdir()
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "file:///C:/x",
            "http://evil.com/x",
        ):
            assert _download_and_extract(url, "123", cfg) is None
        assert opener.urls == []

    def test_html_redirect_to_private_target_is_not_opened(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A scraped page that links to a cloud-metadata address: the recursive
        hop is re-validated, so no second request may leave the process."""
        html = (
            b"<!DOCTYPE html><html><body>"
            b'<a href="http://169.254.169.254/latest/meta-data/123">Download</a>'
            b"</body></html>"
        ) + b" " * 400
        opener = _FakeOpener(_FakeResp(html))  # only ONE response queued
        monkeypatch.setattr("rwmod.skymods._shared_opener", opener)
        cfg = Config(mods_dir=tmp_path / "Mods")
        (tmp_path / "Mods").mkdir()
        assert _download_and_extract("https://smods.ru/x.zip", "123", cfg) is None
        assert opener.urls == ["https://smods.ru/x.zip"]

    def test_http_redirect_to_loopback_is_followed(self, tmp_path: Path, monkeypatch) -> None:
        """The one-hop URL guard does not cover urllib's *internal* redirect
        following: a 302 from an allowlisted host to a loopback/private
        address is chased without ever consulting ``_is_safe_download_url``
        for the new target."""
        hits: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/start.zip":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/meta")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                hits.append(self.path)
                body = b"PK\x03\x04" + b"A" * 600
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPRedirectHandler(),
                urllib.request.HTTPCookieProcessor(),
            )
            monkeypatch.setattr("rwmod.skymods._shared_opener", opener)
            start = f"http://127.0.0.1:{server.server_port}/start.zip"
            checked: list[str] = []

            def guard(url: str) -> bool:
                checked.append(url)
                return url == start  # only the original URL is "allowlisted"

            monkeypatch.setattr("rwmod.skymods._is_safe_download_url", guard)
            cfg = Config(mods_dir=tmp_path / "Mods")
            (tmp_path / "Mods").mkdir()
            _download_and_extract(start, "123", cfg)
            assert checked == [start]
            assert hits == [], f"redirect target was contacted: {hits}"
        finally:
            server.shutdown()
            server.server_close()

    def test_redirect_recursion_depth_is_capped(self, tmp_path: Path, monkeypatch) -> None:
        """HTML hop chains must stop at _MAX_REDIRECTS instead of recursing
        forever (A→B→A loop)."""
        monkeypatch.setattr("rwmod.skymods._is_safe_download_url", lambda url: True)
        calls: list[str] = []

        class _LoopOpener:
            def open(self, req: Any, timeout: float | None = None) -> _FakeCtx:
                calls.append(req.full_url)
                n = len(calls)
                html = (
                    b"<!DOCTYPE html><html><body>"
                    + f'<a href="https://smods.ru/next{n}/123">Download</a>'.encode()
                    + b"</body></html>"
                ) + b" " * 400
                return _FakeCtx(_FakeResp(html))

        monkeypatch.setattr("rwmod.skymods._shared_opener", _LoopOpener())
        cfg = Config(mods_dir=tmp_path / "Mods")
        (tmp_path / "Mods").mkdir()
        assert _download_and_extract("https://smods.ru/a.zip", "123", cfg) is None
        assert len(calls) <= 8


class TestSkymodsSizeCaps:
    def test_search_page_over_cap_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        """The scraped search page is capped at MAX_PAGE_BYTES; an oversized
        (or endless) response must be dropped, not parsed."""
        from unittest.mock import MagicMock

        from rwmod.skymods import MAX_PAGE_BYTES, try_skymods

        resp = _FakeResp(b"<html>" + b"x" * (MAX_PAGE_BYTES + 10))
        urlopen = MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=resp)))
        monkeypatch.setattr("rwmod.skymods.urllib.request.urlopen", urlopen)
        assert try_skymods("123", Config(mods_dir=tmp_path / "Mods")) is None

    def test_scraped_private_download_url_never_opened(self, tmp_path: Path, monkeypatch) -> None:
        """A download link scraped from page content must be SSRF-checked
        before it is opened — a metadata-IP link must not produce a request."""
        from unittest.mock import MagicMock

        from rwmod.skymods import try_skymods

        html = (
            b'<html><a class="skymods-excerpt-btn" '
            b'href="http://169.254.169.254/latest/meta-data/">D</a></html>'
        )
        resp = _FakeResp(html)
        urlopen = MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=resp)))
        monkeypatch.setattr("rwmod.skymods.urllib.request.urlopen", urlopen)
        opener = _FakeOpener()  # tripwire: any open() raises
        monkeypatch.setattr("rwmod.skymods._shared_opener", opener)
        assert try_skymods("123", Config(mods_dir=tmp_path / "Mods")) is None
        assert opener.urls == []

    def test_plain_stream_over_cap_raises(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("rwmod.skymods.MAX_RESPONSE_BYTES", 1000)
        data = b"PK\x03\x04" + b"A" * 5000
        sniff = data[:512]
        with pytest.raises(_DownloadTooBig):
            _stream_to_file(_FakeResp(data[512:]), sniff, tmp_path / "out.bin")

    def test_gzip_stream_over_cap_raises(self, tmp_path: Path, monkeypatch) -> None:
        """gzip is decompressed on the fly; the cap must count decompressed
        bytes, not the (tiny) compressed ones."""
        monkeypatch.setattr("rwmod.skymods.MAX_RESPONSE_BYTES", 1000)
        blob = gzip.compress(b"A" * 500_000)
        sniff = blob[:512]
        with pytest.raises(_DownloadTooBig):
            _stream_to_file(_FakeResp(blob[512:]), sniff, tmp_path / "out.gz")
        assert (tmp_path / "out.gz").stat().st_size <= 1500  # no runaway write


# ── secrets, headers, CORS ─────────────────────────────────────────

_SECRET = "sk_live_PROBE_secret_9f8e7d"


class TestSecretsAndHeaders:
    def test_config_endpoint_never_echoes_api_key(self, client: TestClient) -> None:
        assert client.post("/api/config", json={"steam_api_key": _SECRET}).status_code == 200
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert _SECRET not in resp.text
        assert resp.json()["has_steam_api_key"] is True

    def test_security_headers_present(self, client: TestClient) -> None:
        resp = client.get("/api/config")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Cache-Control"] == "no-store"

    def test_cors_lookalike_origin_not_echoed(self, client: TestClient) -> None:
        for origin in (
            "http://localhost.evil.com",
            "http://127.0.0.1.evil.com",
            "http://evil.com/?x=http://localhost",
        ):
            resp = client.get("/api/config", headers={"Origin": origin})
            assert "access-control-allow-origin" not in resp.headers, origin

    def test_cors_localhost_origin_allowed(self, client: TestClient) -> None:
        resp = client.get("/api/config", headers={"Origin": "http://localhost:5173"})
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
