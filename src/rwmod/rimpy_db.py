"""Community Rules database — community-maintained load-order rules.

The old RimPy ModManager database (``rimpy_database.json``) is dead upstream;
the maintained successor is the CC0-licensed RimSort Community Rules Database.
This module downloads/caches that document and exposes it in the shapes the
rest of rwmod already consumes:

1. Known incompatible mod pairs (packageId based, plus the legacy workshop-id
   ``conflicts`` list).
2. Load-order rules (loadAfter / loadBefore / loadTop / loadBottom).
3. Mod metadata enrichment (legacy ``mods`` map, workshop-id keyed).

Cache contract (``~/.rwmod/community_rules.json``):
* 24 h TTL;
* 2 MB hard cap for both the cache file and a downloaded payload;
* conditional GET with ``If-None-Match`` when an ETag is known (a 304 merely
  refreshes the TTL);
* atomic writes (temp file + :func:`os.replace`);
* fail-open — a network/parse failure uses the stale cache, then the built-in
  (empty) rule set. A failed refresh never makes sorting fail.

Read-only callers that must stay offline (``sort_mods`` / ``check_load_order``)
use :meth:`RimPyDB.load_cached`, which never touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rwmod.mod_rules import (
    find_explicit_conflicts,
    find_rule_conflicts,
    parse_rule_document,
)

__all__ = [
    "CACHE_TTL",
    "COMMUNITY_RULES_URL",
    "DATABASE_URL",
    "MAX_CACHE_BYTES",
    "RimPyDB",
    "refresh_community_rules",
]

_log = logging.getLogger(__name__)

COMMUNITY_RULES_URL = (
    "https://raw.githubusercontent.com/RimSort/Community-Rules-Database/main/communityRules.json"
)
#: Backwards-compatible name for the (now redirected) database URL.
DATABASE_URL = COMMUNITY_RULES_URL

CACHE_FILENAME = "community_rules.json"
LEGACY_CACHE_FILENAME = "rimpy_database.json"
CACHE_TTL = 24 * 3600  # 24 hours
MAX_CACHE_BYTES = 2 * 1024 * 1024  # 2 MiB
_HTTP_TIMEOUT = 30


def _header_value(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name, "")
    except AttributeError:
        return ""
    return str(value or "").strip()


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (crash-safe replace)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class RimPyDB:
    """Singleton accessor for the community rules database.

    The class name is kept for callers written against the old RimPy database;
    the data source is now the RimSort Community Rules Database.
    """

    _instance: RimPyDB | None = None

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir is not None else (Path.home() / ".rwmod")
        self._cache_path = self._cache_dir / CACHE_FILENAME
        self._legacy_cache_path = self._cache_dir / LEGACY_CACHE_FILENAME
        self._etag_path = self._cache_path.with_suffix(".etag")
        self._data: dict[str, Any] = {}
        self._parsed_rules: dict[str, dict[str, Any]] = {}
        self._parsed_conflicts: list[dict] = []
        self._loaded: bool = False
        self._load_attempted: bool = False

    @classmethod
    def get(cls, cache_dir: Path | None = None) -> RimPyDB:
        if cls._instance is None:
            cls._instance = cls(cache_dir)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    # ── public API ──────────────────────────────────────────────────

    def ensure_loaded(self, allow_network: bool = True) -> bool:
        """Load the database, downloading only when ``allow_network`` is set.

        A fresh cache wins outright. When the cache is stale (or missing) and
        network access is allowed, a conditional GET refreshes it. Any network
        failure falls back to the stale cache — fail-open, so callers always
        get the best rules available instead of an exception.
        """
        if self._loaded:
            return True
        if self._load_attempted:
            return bool(self._data)
        self._load_attempted = True

        if self._load_from_cache(check_ttl=True):
            self._loaded = True
            return True

        if allow_network and self._download():
            self._loaded = True
            return True

        # Fail-open: an expired cache still beats an empty rule set.
        if self._load_from_cache(check_ttl=False):
            self._loaded = True
            return True

        _log.warning("Community rules database unavailable — using built-in rules only")
        return False

    def load_cached(self) -> bool:
        """Load from disk without ever touching the network (TTL ignored).

        This is the rule source for sorting/diagnostics: those paths must be
        deterministic and offline-safe. Explicit refreshes go through
        :meth:`ensure_loaded` / :func:`refresh_community_rules`.
        """
        if self._loaded:
            return True
        if self._load_from_cache(check_ttl=False):
            self._loaded = True
            return True
        return False

    @property
    def rules(self) -> dict[str, dict[str, Any]]:
        """Parsed rule map keyed by normalized packageId (disk-only lazy load)."""
        if not self._loaded:
            self.load_cached()
        return self._parsed_rules

    def get_conflicts(self, workshop_ids: set[str]) -> list[dict]:
        """Find known conflicts among the given workshop IDs (legacy schema)."""
        if not self.ensure_loaded():
            return []
        conflicts: list[dict] = []
        rules = self._data.get("conflicts", []) if isinstance(self._data, dict) else []
        if not isinstance(rules, list):
            return []
        ids_set = set(workshop_ids)
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            a = str(rule.get("a", "") or rule.get("mod_a", "") or rule.get("id1", ""))
            b = str(rule.get("b", "") or rule.get("mod_b", "") or rule.get("id2", ""))
            if a in ids_set and b in ids_set:
                conflicts.append(
                    {
                        "mod_a": a,
                        "mod_b": b,
                        "reason": str(
                            rule.get("reason", "")
                            or rule.get("description", "")
                            or "Known conflict"
                        ),
                    }
                )
        return conflicts

    def get_incompatible(self, package_ids: set[str]) -> list[dict]:
        """Return declared incompatibilities among the given packageIds."""
        if not self.ensure_loaded():
            return []
        found = find_rule_conflicts(self._parsed_rules, package_ids)
        found += find_explicit_conflicts(self._parsed_conflicts, package_ids)
        unique: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for conflict in found:
            pair = (conflict["a"].lower(), conflict["b"].lower())
            key = (min(pair), max(pair))
            if key in seen:
                continue
            seen.add(key)
            unique.append(conflict)
        return unique

    def get_load_rules(self, workshop_ids: set[str]) -> list[dict]:
        """Get load-order rules (before/after relationships) — legacy schema."""
        if not self.ensure_loaded():
            return []
        rules = self._data.get("load_rules", []) if isinstance(self._data, dict) else []
        if not isinstance(rules, list):
            return []
        ids_set = set(workshop_ids)
        result: list[dict] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            before = rule.get("before", [])
            after = rule.get("after", [])
            if not isinstance(before, list):
                before = []
            if not isinstance(after, list):
                after = []
            mid = str(rule.get("id", "") or rule.get("mod_id", ""))
            if mid in ids_set or any(str(x) in ids_set for x in before + after):
                result.append(
                    {
                        "mod_id": mid,
                        "before": [str(x) for x in before],
                        "after": [str(x) for x in after],
                        "note": str(rule.get("note", "")),
                    }
                )
        return result

    def search_by_workshop_id(self, workshop_id: str) -> dict | None:
        """Look up a workshop ID for metadata enrichment."""
        if not self.ensure_loaded():
            return None
        mods = self._data.get("mods", {}) if isinstance(self._data, dict) else {}
        if isinstance(mods, dict):
            return mods.get(str(workshop_id))
        return None

    # ── internal ───────────────────────────────────────────────────

    def _parse_payload(self, data: object) -> bool:
        if not isinstance(data, dict) or not data:
            return False
        rules, conflicts = parse_rule_document(data)
        legacy_useful = bool(data.get("load_rules") or data.get("mods") or data.get("conflicts"))
        if not rules and not conflicts and not legacy_useful:
            return False
        self._data = data
        self._parsed_rules = rules
        self._parsed_conflicts = conflicts
        return True

    def _load_from_cache(self, *, check_ttl: bool) -> bool:
        for path in (self._cache_path, self._legacy_cache_path):
            if not path.exists():
                continue
            try:
                if path.stat().st_size > MAX_CACHE_BYTES:
                    _log.warning("Ignoring oversized community rules cache: %s", path)
                    continue
                if check_ttl and time.time() - path.stat().st_mtime > CACHE_TTL:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if self._parse_payload(data):
                    _log.info(
                        "Community rules loaded from cache %s (%d KB)",
                        path.name,
                        path.stat().st_size // 1024,
                    )
                    return True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                _log.debug("Community rules cache read failed (%s): %s", path.name, e)
        return False

    def _read_etag(self) -> str:
        try:
            return self._etag_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def _touch_cache(self) -> None:
        for path in (self._cache_path, self._legacy_cache_path):
            if path.exists():
                try:
                    os.utime(path, None)
                except OSError:
                    return

    def _download(self) -> bool:
        _log.info("Downloading community rules database...")
        try:
            headers = {"User-Agent": "rwmod/1.0"}
            etag = self._read_etag()
            if etag:
                headers["If-None-Match"] = etag
            req = Request(COMMUNITY_RULES_URL, headers=headers)
            with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310 — HTTPS-only URL
                status = getattr(resp, "status", 200)
                if status == 304:
                    # Not modified: refresh the cache TTL and keep the data.
                    self._touch_cache()
                    return self._load_from_cache(check_ttl=False)
                raw = resp.read()
                response_etag = _header_value(resp, "ETag")

            if len(raw) > MAX_CACHE_BYTES:
                _log.warning(
                    "Community rules payload too large (%d bytes > %d)",
                    len(raw),
                    MAX_CACHE_BYTES,
                )
                return False
            data = json.loads(raw)
            if not self._parse_payload(data):
                _log.warning("Community rules payload has an unsupported schema")
                return False

            self._cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                _atomic_write(
                    self._cache_path,
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                )
                if response_etag:
                    _atomic_write(self._etag_path, response_etag)
            except OSError as e:
                # The in-memory copy is still usable; don't fail the load.
                _log.warning("Could not write community rules cache: %s", e)
            _log.info("Community rules downloaded (%d KB)", len(raw) // 1024)
            return True
        except HTTPError as e:
            # urllib raises HTTPError for 304 instead of returning a response.
            if e.code == 304:
                self._touch_cache()
                return self._load_from_cache(check_ttl=False)
            _log.warning("Failed to download community rules: %s", e)
            return False
        except Exception as e:  # noqa: BLE001 — fail-open by contract
            _log.warning("Failed to download community rules: %s", e)
            return False


def refresh_community_rules() -> bool:
    """Force a network refresh of the cached Community Rules database."""
    db = RimPyDB.get()
    db._loaded = False
    db._load_attempted = False
    return db.ensure_loaded(allow_network=True)
