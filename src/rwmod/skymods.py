"""Skymods fallback — download mods from smods.ru when SteamCMD fails."""

from __future__ import annotations

import gzip
import io
import ipaddress
import logging
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Buffer
from http.client import HTTPResponse
from pathlib import Path
from typing import Any

__all__ = ["try_skymods", "skymods_status", "reset_skymods_breaker"]

from rwmod.config import Config
from rwmod.utils import safe_extract_zip

_log = logging.getLogger(__name__)

SKYMODS_SEARCH = "https://catalogue.smods.ru/"
RIMWORLD_APP_ID = "294100"

# ── circuit breaker (P2-2) ─────────────────────────────────────────
# smods.ru is behind a Cloudflare JS challenge: plain urllib gets a 403
# ("Cf-Mitigated: challenge"). Retrying it per failed mod wastes time and
# floods the log, so the first failure opens a 10-minute breaker. The
# fallback is also opt-in (Config.enable_skymods defaults to False).
SKYMODS_BLOCK_SECONDS = 600

_breaker_lock = threading.Lock()
_breaker_until = 0.0
_breaker_logged = False


def reset_skymods_breaker() -> None:
    """Close the breaker and forget the one-shot log state (tests/DI)."""
    global _breaker_until, _breaker_logged
    with _breaker_lock:
        _breaker_until = 0.0
        _breaker_logged = False


def skymods_blocked() -> bool:
    """True while the breaker is open; also clears it once it expires."""
    global _breaker_until, _breaker_logged
    with _breaker_lock:
        if _breaker_until and time.monotonic() >= _breaker_until:
            _breaker_until = 0.0
            _breaker_logged = False
            return False
        return _breaker_until > 0


def _mark_skymods_blocked(reason: str) -> None:
    """Open the breaker for 10 minutes, logging only on the transition."""
    global _breaker_until, _breaker_logged
    with _breaker_lock:
        if not _breaker_logged:
            _log.warning("Skymods 备用源不可用，%s 秒内不再尝试：%s", SKYMODS_BLOCK_SECONDS, reason)
            _breaker_logged = True
        _breaker_until = time.monotonic() + SKYMODS_BLOCK_SECONDS


def skymods_status(config: Config | None = None) -> str:
    """One of ``disabled`` / ``blocked`` / ``available`` (for /api/status)."""
    if config is None:
        config = Config.load()
    if not bool(getattr(config, "enable_skymods", False)):
        return "disabled"
    return "blocked" if skymods_blocked() else "available"


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Download URLs are scraped from third-party page content, so we restrict the
# scheme + hostname before opening them (SSRF guard: no file://, no private/
# loopback/link-local targets, no non-whitelisted domains).
_ALLOWED_DOWNLOAD_DOMAINS = ("smods.ru", "modsbase.com")
MAX_PAGE_BYTES = 2 * 1024 * 1024  # search/redirect page HTML cap
MAX_RESPONSE_BYTES = 1024 * 1024 * 1024  # 1 GB cap on fallback mod downloads
_STREAM_CHUNK = 1 << 20  # 1 MiB chunks while streaming to disk


class _DownloadTooBig(Exception):
    """Raised when a streamed download exceeds MAX_RESPONSE_BYTES."""


def _host_allowed(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _ALLOWED_DOWNLOAD_DOMAINS)


def _is_safe_download_url(url: str) -> bool:
    """Reject SSRF vectors (file://, intranet, metadata hosts) before opening."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not _host_allowed(host):
        return False
    # Refuse hosts that resolve to private / loopback / link-local addresses
    # (cloud metadata like 169.254.169.254, LAN scanners, localhost).
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(host))
    except OSError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when the *new* URL passes the SSRF guard.

    urllib follows 3xx internally, so the one check in
    ``_download_and_extract`` only ever saw the first hop: a 302 from an
    allowlisted host to a loopback / cloud-metadata address was chased without
    validation. Re-check the host allowlist (cheap, no DNS) and then the full
    guard (DNS-resolved private ranges) before urllib issues the next request.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            parsed = urllib.parse.urlparse(newurl)
        except ValueError:
            parsed = None
        host = ((parsed.hostname or "") if parsed else "").lower()
        if parsed is None or parsed.scheme not in ("http", "https") or not _host_allowed(host):
            _log.warning("Skymods 拒绝不安全的重定向: %s", newurl[:120])
            return None
        if not _is_safe_download_url(newurl):
            _log.warning("Skymods 重定向目标解析到内网地址，拒绝: %s", newurl[:120])
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _harden_opener(opener: Any) -> Any:
    """Make an opener refuse unsafe redirects (idempotent, DI-friendly).

    Production builds ``_shared_opener`` with ``_SafeRedirectHandler``; this
    also covers injected/test openers built with the plain
    ``HTTPRedirectHandler``. Only real ``OpenerDirector``-shaped objects are
    touched; test doubles that merely expose ``open()`` are returned as-is.
    """
    handlers = getattr(opener, "handlers", None)
    if not isinstance(handlers, list):
        return opener
    if any(isinstance(h, _SafeRedirectHandler) for h in handlers):
        return opener
    safe = _SafeRedirectHandler()
    opener.add_handler(safe)  # sets parent + dispatch tables
    # add_handler registers the safe handler next to the plain one; the plain
    # one would win the 3xx chain (it was inserted first), so drop it from the
    # dispatch tables (the *only* thing _call_chain consults).
    http_errors = getattr(opener, "handle_error", {}).get("http", {})
    for code, chain in list(http_errors.items()):
        http_errors[code] = [
            h
            for h in chain
            if not (
                isinstance(h, urllib.request.HTTPRedirectHandler)
                and not isinstance(h, _SafeRedirectHandler)
            )
        ]
    return opener


# Shared opener with redirect + cookie handling — reused across all Skymods
# downloads. Building a new opener per download wastes resources and loses
# TCP keep-alive / connection pooling.
_shared_opener = urllib.request.build_opener(
    _SafeRedirectHandler(),
    urllib.request.HTTPCookieProcessor(),
)


def try_skymods(mod_id: str, config: Config) -> Path | None:
    """Try to download a mod from Skymods as fallback.

    Returns the path to the extracted mod folder on success, None on failure.
    Disabled by default; a failed search opens the 10-minute breaker.
    """
    if not bool(getattr(config, "enable_skymods", False)):
        return None
    if skymods_blocked():
        return None

    search_url = f"{SKYMODS_SEARCH}?s={mod_id}&app={RIMWORLD_APP_ID}"

    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 — HTTPS-only Skymods URL
            page = resp.read(MAX_PAGE_BYTES + 1)
            if len(page) > MAX_PAGE_BYTES:
                _log.warning("Skymods 搜索页过大，放弃 (%s)", mod_id)
                return None
            html = page.decode("utf-8", errors="replace")
    except OSError as e:
        # One log line per breaker window, not one per failed mod.
        _mark_skymods_blocked(str(e))
        return None

    download_url = _extract_download_url(html, mod_id)
    if not download_url:
        _log.info("Skymods 未找到下载链接: %s", mod_id)
        return None

    return _download_and_extract(download_url, mod_id, config)


def _extract_download_url(html: str, mod_id: str) -> str | None:
    """Extract the mod download URL from Skymods search results page."""
    # Method 1: Look for skymods-excerpt-btn link
    m = re.search(r'<a[^>]*class="[^"]*skymods-excerpt-btn[^"]*"[^>]*href="([^"]+)"', html)
    if m:
        return m.group(1)

    # Method 2: Look for generic download button
    m = re.search(r'<a[^>]*href="([^"]*modsbase[^"]*)"', html)
    if m:
        return m.group(1)

    # Method 3: Look for any link containing the mod_id and download-related text
    for pattern in [
        rf'href="([^"]*{mod_id}[^"]*)"[^>]*>.*?(?:download|скачать|Download)',
        r'href="(https://modsbase\.com[^"]+)"',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)

    return None


# Max redirect hops when Skymods returns an HTML page instead of a zip.
# Guards against redirect loops (A→B→A) that would otherwise recurse forever.
_MAX_REDIRECTS = 5


def _stream_to_file(resp: HTTPResponse, sniff: bytes, tmp_path: Path) -> tuple[str, int]:
    """Stream a response body to ``tmp_path`` in chunks.

    Returns ``(kind, total_bytes)`` where kind is ``"zip"`` (or ``"raw"``)
    on success. Raises _DownloadTooBig when the body exceeds
    MAX_RESPONSE_BYTES, and OSError on IO problems.
    """
    total = 0
    with open(tmp_path, "wb") as fh:
        # gzip-wrapped zips (some CDNs) are decompressed on the fly so a
        # multi-GB mod never lands in RAM — it streams disk → disk.
        if sniff[:2] == b"\x1f\x8b":

            class _ChainReader(io.RawIOBase):
                """Reads the sniffed head first, then the live response."""

                def __init__(self, head: bytes, tail: HTTPResponse) -> None:
                    super().__init__()
                    self._head = io.BytesIO(head)
                    self._tail = tail

                def readinto(self, b: Buffer) -> int:
                    n = self._head.readinto(b)
                    if n:
                        return n
                    return self._tail.readinto(b)

            gz = gzip.GzipFile(fileobj=_ChainReader(sniff, resp))
            while True:
                chunk = gz.read(_STREAM_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise _DownloadTooBig
                fh.write(chunk)
            return "zip", total

        fh.write(sniff)
        total = len(sniff)
        while True:
            chunk = resp.read(_STREAM_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise _DownloadTooBig
            fh.write(chunk)
    return "raw", total


def _download_and_extract(
    url: str,
    mod_id: str,
    config: Config,
    _depth: int = 0,
) -> Path | None:
    """Download from Skymods and extract to mods directory.
    Handles redirects, gzip compression, and non-zip responses gracefully."""
    if _depth > _MAX_REDIRECTS:
        _log.warning("Skymods 重定向次数过多，放弃 (%s)", mod_id)
        return None

    # SSRF guard: the URL comes from third-party page content.
    if not _is_safe_download_url(url):
        _log.warning("Skymods 下载 URL 不合法，拒绝 (%s): %s", mod_id, url[:120])
        return None

    tmp_path: Path | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        opener = _harden_opener(_shared_opener)
        with opener.open(req, timeout=60) as resp:
            sniff = resp.read(512)
            if len(sniff) < 512:
                _log.warning("Skymods 响应内容过短 (%s): %s bytes", mod_id, len(sniff))
                return None

            # Detect HTML responses — likely a redirect page or error
            if sniff.startswith(b"<!") or sniff.startswith(b"<html") or sniff.startswith(b"<HTML"):
                page = sniff + resp.read(MAX_PAGE_BYTES)
                text = page.decode("utf-8", errors="replace")
                redirect_url = _extract_download_url(text, mod_id)
                if redirect_url and redirect_url != url:
                    _log.info("Skymods 重定向: %s → %s", url[:80], redirect_url[:80])
                    return _download_and_extract(redirect_url, mod_id, config, _depth + 1)
                _log.warning("Skymods 返回 HTML 而非 zip (%s): %s", mod_id, text[:200])
                return None

            # Stream the body (gzip-aware) to a temp file — bounded by
            # MAX_RESPONSE_BYTES, never buffered fully in RAM.
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                kind, _size = _stream_to_file(resp, sniff, tmp_path)
            except _DownloadTooBig:
                _log.warning("Skymods 下载内容过大，放弃 (%s)", mod_id)
                return None
            _log.debug("Skymods 已流式落盘 (%s): kind=%s", mod_id, kind)

        if not zipfile.is_zipfile(tmp_path):
            _log.warning(
                "Skymods 下载内容不是 zip (%s): 首字节 %s",
                mod_id,
                sniff[:16].hex(),
            )
            return None

        # Extract to temp directory (with path-traversal protection)
        extract_dir = Path(tempfile.mkdtemp())
        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                safe_extract_zip(zf, extract_dir)
        except (zipfile.BadZipFile, OSError) as e:
            _log.warning("Skymods zip 解压失败 (%s): %s", mod_id, e)
            return None

        mod_folder = _find_mod_folder(extract_dir)
        if not mod_folder:
            _log.warning("Skymods zip 中未找到 Mod 文件夹: %s", mod_id)
            return None

        from rwmod.downloader import _pick_folder_name

        folder_name = _pick_folder_name(mod_folder, mod_id)
        dest = config.mods_dir / folder_name
        if dest.exists():
            dest = config.mods_dir / f"{folder_name}_skymods"
        shutil.copytree(mod_folder, dest)
        _log.info("Skymods 下载成功: %s → %s", mod_id, folder_name)
        return dest
    except (OSError, zipfile.BadZipFile) as e:
        _log.warning("Skymods 下载/解压失败 (%s): %s", mod_id, e)
        return None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        if "extract_dir" in locals() and extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)


def _find_mod_folder(extract_dir: Path) -> Path | None:
    """Find the actual mod folder inside extracted zip contents."""
    # Check if About.xml exists directly
    if (extract_dir / "About" / "About.xml").exists():
        return extract_dir

    # Check one level deep
    for d in extract_dir.iterdir():
        if d.is_dir() and (d / "About" / "About.xml").exists():
            return d

    # Check two levels deep
    for d in extract_dir.iterdir():
        if d.is_dir():
            for sub in d.iterdir():
                if sub.is_dir() and (sub / "About" / "About.xml").exists():
                    return sub

    # If no About.xml found, return the first subdirectory
    dirs = [d for d in extract_dir.iterdir() if d.is_dir()]
    if dirs:
        return dirs[0]

    return None
