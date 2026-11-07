"""Steam Workshop database — optional, opt-in metadata enrichment.

Source: the RimSort Steam Workshop Database (CC0-1.0),
``https://raw.githubusercontent.com/RimSort/Steam-Workshop-Database/main/steamDB.json``
(~47 MB, 57k workshop entries). Schema::

    {"version": <epoch>, "database": {<workshop_id>: {
        "url", "steamName", "packageId", "name", "authors",
        "dependencies": {<workshop_id>: [<name>, <url>]},
        "gameVersions": ["1.5", ...],
        "tags": [{"tag": "...", "display_name": "..."}],
        "unpublished": bool, "appid": bool}}}

This is deliberately **not** the Community Rules database (rimpy_db.py); the
two serve different purposes and have different cache contracts.

Cache contract (``~/.rwmod/steam_db.json``):
* 30-day TTL (reported as ``stale``; a stale cache is still usable — queries
  never fail just because a refresh is due);
* 60 MB hard cap, overridable per call / from ``Config.steam_db_max_bytes``;
* conditional GET with ``If-None-Match`` (304 refreshes the TTL in place);
* atomic writes (temp file + :func:`os.replace`);
* fail-open — a network/parse failure keeps the previous cache and never
  makes a caller fail.

Nothing here touches the network implicitly. Only :meth:`SteamDB.refresh`
downloads, and it is invoked exclusively by the explicit
``POST /api/rimsort/steamdb/refresh`` endpoint (or a direct caller). Sorting
and load-order diagnostics use :meth:`SteamDB.load_cached`, which is disk-only.

The 47 MB document is parsed at most once per process: queries go through a
workshop-id map plus a case-insensitive packageId index built during that one
parse, so the raw document is never re-read per request.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rwmod.mod_rules import normalize_package_id

__all__ = [
    "CACHE_FILENAME",
    "CACHE_TTL",
    "MAX_CACHE_BYTES",
    "STEAM_DB_URL",
    "SteamDB",
]

_log = logging.getLogger(__name__)

STEAM_DB_URL = "https://raw.githubusercontent.com/RimSort/Steam-Workshop-Database/main/steamDB.json"

CACHE_FILENAME = "steam_db.json"
CACHE_TTL = 30 * 24 * 3600  # 30 days
MAX_CACHE_BYTES = 60 * 1024 * 1024  # 60 MiB
_HTTP_TIMEOUT = 120  # the real payload is ~47 MB


def _read_limited(response: Any, max_bytes: int) -> bytes | None:
    """Read at most ``max_bytes`` from an HTTP response, streaming.

    The previous ``resp.read()`` buffered the entire body and only then
    checked its length, so the documented 60 MB cap never protected memory.
    Chunks are read against the remaining budget; the first byte past the cap
    aborts the read and returns ``None``. Minimal response doubles that do
    not accept a size argument fall back to one bounded check.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = response.read(max_bytes + 1 - total)
        except TypeError:
            # Test doubles / stripped-down response objects take no size arg.
            raw = response.read()
            return raw if len(raw) <= max_bytes else None
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _header_value(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name, "")
    except AttributeError:
        return ""
    return str(value or "").strip()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (crash-safe replace)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _normalize_entry(workshop_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Reduce one raw steamDB entry to the fields this module exposes."""
    dependencies: dict[str, str] = {}
    raw_dependencies = entry.get("dependencies")
    if isinstance(raw_dependencies, dict):
        for wid, value in raw_dependencies.items():
            name = ""
            if isinstance(value, (list, tuple)) and value:
                name = str(value[0])
            elif isinstance(value, str):
                name = value
            dependencies[str(wid)] = name

    tags: list[str] = []
    raw_tags = entry.get("tags")
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, str):
                tags.append(tag)
            elif isinstance(tag, dict):
                value = tag.get("tag") or tag.get("display_name")
                if isinstance(value, str) and value:
                    tags.append(value)

    raw_versions = entry.get("gameVersions")
    game_versions = [str(v) for v in raw_versions] if isinstance(raw_versions, list) else []

    package_id = entry.get("packageId") or entry.get("packageid") or ""
    name = entry.get("name") or entry.get("steamName") or ""
    authors = entry.get("authors") or ""

    return {
        "workshop_id": workshop_id,
        "package_id": str(package_id),
        "name": str(name),
        "authors": str(authors),
        "game_versions": game_versions,
        "dependencies": dependencies,
        "tags": tags,
    }


class SteamDB:
    """Lazy accessor for the cached Steam Workshop database.

    Construction performs no I/O. :meth:`get` returns a process singleton, but
    rebuilds it when the effective cache directory changes (tests patch
    ``Path.home``); it therefore never leaks a closed-over home between
    requests or test cases.
    """

    _instance: SteamDB | None = None

    def __init__(self, cache_dir: Path | None = None, *, max_bytes: int = MAX_CACHE_BYTES) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir is not None else (Path.home() / ".rwmod")
        self._cache_path = self._cache_dir / CACHE_FILENAME
        self._etag_path = self._cache_path.with_suffix(".etag")
        self._max_bytes = max(1, int(max_bytes))
        self._by_wid: dict[str, dict[str, Any]] = {}
        self._by_pkg: dict[str, str] = {}
        self._version: int | None = None
        self._loaded = False

    @classmethod
    def get(cls, cache_dir: Path | None = None) -> SteamDB:
        target = Path(cache_dir) if cache_dir is not None else (Path.home() / ".rwmod")
        if cls._instance is None or cls._instance._cache_dir != target:
            cls._instance = cls(target)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    # ── public API ──────────────────────────────────────────────────

    def load_cached(self, *, max_bytes: int | None = None) -> bool:
        """Load the cache from disk. Never touches the network, TTL-agnostic.

        Returns ``False`` (fail-open) when the cache is missing, oversized,
        corrupt or has an unsupported schema.
        """
        if self._loaded:
            return True
        return self._load_from_cache(limit=self._limit(max_bytes))

    def query(self, workshop_id: str) -> dict[str, Any] | None:
        """Return normalized metadata for a workshop ID, or ``None``."""
        if not self.load_cached():
            return None
        info = self._by_wid.get(str(workshop_id).strip())
        return _copy_info(info) if info is not None else None

    def query_package(self, package_id: str) -> dict[str, Any] | None:
        """Return normalized metadata for a packageId (case-insensitive)."""
        if not self.load_cached():
            return None
        norm = normalize_package_id(package_id)
        if not norm:
            return None
        workshop_id = self._by_pkg.get(norm)
        info = self._by_wid.get(workshop_id) if workshop_id else None
        return _copy_info(info) if info is not None else None

    def refresh(self, *, max_bytes: int | None = None) -> bool:
        """Explicitly download the database into the cache.

        A conditional GET is sent when an ETag is known; a 304 just refreshes
        the cache mtime. Every failure is swallowed (fail-open) and reported as
        ``False`` — the caller decides whether to surface it.
        """
        limit = self._limit(max_bytes)
        _log.info("Downloading Steam Workshop database...")
        try:
            headers = {"User-Agent": "rwmod/1.0"}
            etag = self._read_etag()
            if etag:
                headers["If-None-Match"] = etag
            req = Request(STEAM_DB_URL, headers=headers)
            with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310 — HTTPS-only URL
                status = getattr(resp, "status", 200)
                if status == 304:
                    self._loaded = False
                    self._touch_cache()
                    return self._load_from_cache(limit=limit)
                raw = _read_limited(resp, limit)
                response_etag = _header_value(resp, "ETag")

            if raw is None:
                _log.warning(
                    "Steam Workshop database payload exceeds the %d-byte cap",
                    limit,
                )
                return False
            data = json.loads(raw)
            if not self._parse_payload(data):
                _log.warning("Steam Workshop database payload has an unsupported schema")
                return False

            self._cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                # The payload is already valid JSON — write it verbatim instead
                # of re-serializing 47 MB.
                _atomic_write_bytes(self._cache_path, raw)
                if response_etag:
                    _atomic_write_text(self._etag_path, response_etag)
            except OSError as e:
                # The in-memory copy is still usable; don't fail the refresh.
                _log.warning("Could not write Steam Workshop database cache: %s", e)
            _log.info("Steam Workshop database downloaded (%d KB)", len(raw) // 1024)
            return True
        except HTTPError as e:
            # urllib raises HTTPError for 304 instead of returning a response.
            if e.code == 304:
                self._loaded = False
                self._touch_cache()
                return self._load_from_cache(limit=limit)
            _log.warning("Failed to download Steam Workshop database: %s", e)
            return False
        except Exception as e:  # noqa: BLE001 — fail-open by contract
            _log.warning("Failed to download Steam Workshop database: %s", e)
            return False

    def status(self, *, max_bytes: int | None = None) -> dict[str, Any]:
        """Report cache state without ever downloading.

        Keys: ``cached`` (file exists), ``valid`` (parsed), ``entries``,
        ``version``, ``updated_at`` (ISO-8601 UTC), ``size``, ``stale``,
        ``ttl_seconds`` and ``max_bytes``.
        """
        limit = self._limit(max_bytes)
        payload: dict[str, Any] = {
            "cached": False,
            "valid": False,
            "entries": 0,
            "version": None,
            "updated_at": None,
            "size": 0,
            "stale": False,
            "ttl_seconds": CACHE_TTL,
            "max_bytes": limit,
        }
        try:
            stat = self._cache_path.stat()
        except OSError:
            return payload

        payload["cached"] = True
        payload["size"] = stat.st_size
        payload["updated_at"] = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
        payload["stale"] = (time.time() - stat.st_mtime) > CACHE_TTL
        if self.load_cached(max_bytes=limit):
            payload["valid"] = True
            payload["entries"] = len(self._by_wid)
            payload["version"] = self._version
        return payload

    # ── internal ───────────────────────────────────────────────────

    def _limit(self, max_bytes: int | None) -> int:
        return self._max_bytes if max_bytes is None else max(1, int(max_bytes))

    def _parse_payload(self, data: object) -> bool:
        if not isinstance(data, dict):
            return False
        database = data.get("database")
        if not isinstance(database, dict) or not database:
            return False

        by_wid: dict[str, dict[str, Any]] = {}
        by_pkg: dict[str, str] = {}
        for raw_id, entry in database.items():
            if not isinstance(entry, dict):
                continue
            workshop_id = str(raw_id)
            info = _normalize_entry(workshop_id, entry)
            by_wid[workshop_id] = info
            norm = normalize_package_id(info["package_id"])
            if norm:
                by_pkg.setdefault(norm, workshop_id)
        if not by_wid:
            return False

        version = data.get("version")
        self._by_wid = by_wid
        self._by_pkg = by_pkg
        self._version = version if isinstance(version, int) else None
        self._loaded = True
        return True

    def _load_from_cache(self, *, limit: int) -> bool:
        try:
            stat = self._cache_path.stat()
        except OSError:
            return False
        if stat.st_size > limit:
            _log.warning("Ignoring oversized Steam Workshop database cache: %s", self._cache_path)
            return False
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            _log.debug("Steam Workshop database cache read failed: %s", e)
            return False
        if not self._parse_payload(data):
            _log.debug("Steam Workshop database cache has an unsupported schema")
            return False
        _log.info(
            "Steam Workshop database loaded from cache (%d entries, %d KB)",
            len(self._by_wid),
            stat.st_size // 1024,
        )
        return True

    def _read_etag(self) -> str:
        try:
            return self._etag_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def _touch_cache(self) -> None:
        try:
            os.utime(self._cache_path, None)
        except OSError:
            return


def _copy_info(info: dict[str, Any]) -> dict[str, Any]:
    return {
        **info,
        "game_versions": list(info["game_versions"]),
        "dependencies": dict(info["dependencies"]),
        "tags": list(info["tags"]),
    }
