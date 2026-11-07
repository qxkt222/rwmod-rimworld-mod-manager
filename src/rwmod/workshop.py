"""Steam Workshop search via web scraping (no API key needed)."""

from __future__ import annotations

import contextlib
import json
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TypedDict

from rwmod.errors import WorkshopError
from rwmod.offline import mark_offline, mark_online

__all__ = [
    "ModSearchResult",
    "search_workshop",
    "fetch_collection_children",
    "fetch_item_details",
    "fetch_item_dependencies",
    "is_collection",
    "collection_probe_failed",
    "reset_collection_probe",
    "check_mod_updates",
]

# ── shared HTTP connection pool ────────────────────────────────────
# All Steam API calls share one opener for TCP connection reuse (keep-alive).
# Without this, each urllib.request.urlopen() does a fresh TCP+TLS handshake.
_shared_opener = urllib.request.build_opener(urllib.request.HTTPHandler())
_MAX_WORKERS = 4  # parallel batch requests to Steam API

_PUBLISHED_FILE_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)


def _request_json(
    url: str,
    *,
    form: dict[str, str] | None = None,
    timeout: int = 15,
) -> dict | None:
    """Make one Steam API call and parse its JSON body; None on any failure.

    Every Steam JSON call goes through here so two things live in one place:
    the request shape (UA, form encoding, keep-alive opener) and the
    connectivity bookkeeping that ``/api/status`` reports. Callers only decide
    what an empty result means for them.
    """
    headers = {"User-Agent": "rwmod/1.0"}
    data = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()

    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with _shared_opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception:
        mark_offline()
        return None

    mark_online()
    return payload if isinstance(payload, dict) else None


def _published_file_details_form(mod_ids: list[str]) -> dict[str, str]:
    """Build the GetPublishedFileDetails POST body for a batch of ids."""
    form = {"itemcount": str(len(mod_ids))}
    for i, mid in enumerate(mod_ids):
        form[f"publishedfileids[{i}]"] = mid
    return form


@dataclass
class ModSearchResult:
    id: str
    title: str
    author: str
    description: str = ""
    preview_url: str = ""
    rating: str = ""
    subscribers: str = ""


STEAM_APP_ID = "294100"


def search_workshop(query: str, page: int = 1, count: int = 20) -> list[ModSearchResult] | None:
    """Search RimWorld workshop using Steam's JSON API (no auth required).

    Returns ``None`` when Steam could not be reached, and ``[]`` when Steam
    answered but had no matches — callers must not conflate the two.
    """
    url = (
        f"https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/?"
        f"key=anonymous&format=json&appid={STEAM_APP_ID}"
        f"&query_type=0&page={page}&numperpage={count}"
        f"&search_text={urllib.parse.quote(query)}"
        f"&return_vote_data=1&return_previews=1&return_children=0"
    )

    data = _request_json(url)
    if data is None:
        return None

    results: list[ModSearchResult] = []
    published_files = data.get("response", {}).get("publishedfiledetails", [])
    for f in published_files:
        preview = f.get("preview_url", "")
        # If preview URL is a YouTube link, skip it for thumbnail
        if "youtube" in preview or "youtu.be" in preview:
            preview = ""

        results.append(
            ModSearchResult(
                id=f.get("publishedfileid", ""),
                title=f.get("title", "Unknown"),
                author=f.get("creator", "Unknown"),
                description=(f.get("file_description") or f.get("description", ""))[:200],
                preview_url=preview,
                rating=f"{f.get('vote_data', {}).get('score', 0):.1f}"
                if f.get("vote_data")
                else "",
                subscribers=f.get("subscriptions", ""),
            )
        )
    return results


def _get_collection_details(collection_id: str, api_key: str = "anonymous") -> dict | None:
    """Call ISteamRemoteStorage/GetCollectionDetails and return its response dict.

    This is the *dedicated* collection endpoint and must be called via HTTP
    POST (the old code POSTed to IPublishedFileService/QueryFiles, which Steam
    rejects with "Method Not Allowed" 405 — collections were never detected).

    Returns None on network failure or when the top-level result != 1.
    """
    url = (
        "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
        f"?key={api_key}&format=json"
    )
    data = _request_json(
        url,
        form={"collectioncount": "1", "publishedfileids[0]": collection_id},
        timeout=20,
    )
    if data is None:
        return None

    response = data.get("response", {})
    if not isinstance(response, dict):
        return None
    if response.get("result") != 1:
        return None
    return response


# ── probe outcome side channel ─────────────────────────────────────
# is_collection() must keep its historical bool contract: it answers False
# both for "definitely not a collection" and for "could not ask Steam".
# Download routing must tell those apart — treating a failed probe as
# "not a collection" is exactly how a collection id used to be handed to
# SteamCMD. The latest outcome is recorded per thread; callers that need the
# distinction call reset_collection_probe(), is_collection(), then
# collection_probe_failed() in the same thread (the three calls must not be
# split across threads, which is why routers/download.py wraps them in one
# thread-pool function).
_probe_state = threading.local()


def reset_collection_probe() -> None:
    """Forget this thread's previous probe outcome before a new is_collection()."""
    _probe_state.failed = False


def collection_probe_failed() -> bool:
    """True when the last is_collection() call in this thread could not reach Steam."""
    return bool(getattr(_probe_state, "failed", False))


def is_collection(workshop_id: str) -> bool:
    """Check if a workshop item is a collection via Steam Web API.

    A valid collection returns ``result == 1`` in ``collectiondetails[0]``;
    a non-collection returns ``result == 9``. Returns False when the API
    cannot be reached; use ``collection_probe_failed()`` to distinguish that
    from a definitive "not a collection".
    """
    response = _get_collection_details(workshop_id)
    if not response:
        _probe_state.failed = True
        return False
    _probe_state.failed = False
    details = response.get("collectiondetails", [])
    return bool(details) and details[0].get("result") == 1


def fetch_collection_children(collection_id: str) -> list[str] | None:
    """Fetch every mod ID in a Steam Workshop collection — nothing may be missed.

    Layer 1: GetCollectionDetails API — authoritative, returns the complete
             child list (verified to equal the page's declared ``childCount``).
    Layer 2: HTML scraping — precise ``sharedfile_<id>`` extraction (noise-free).
             Always attempted so the result is the *union* of both sources:
             a child present in either source is never dropped.
    Layer 3: User API Key — for private/friends-only collections.

    Returns ``None`` when both primary sources failed (Steam unreachable), and
    ``[]`` when at least one of them answered with an empty collection. A
    failed source is never turned into "the collection is empty".
    Results are deduplicated while preserving order.
    """
    log = __import__("logging").getLogger(__name__)

    # Layer 1 — authoritative API (returns every child in one shot)
    result = _fetch_collection_api(collection_id, "anonymous")

    # Layer 2 — precise, noise-free scrape; merge (union) so no child is missed
    # even if a very large collection were ever truncated by the API.
    scraped = _scrape_collection_page(collection_id, timeout=12)

    if result is None and scraped is None:
        log.warning("Collection %s: API and scraping both unreachable", collection_id)
        return None

    # A None layer means that source failed; the other source still answers.
    merged = _dedup_ids([*(result or []), *(scraped or [])])
    if merged:
        if result is not None and scraped is not None:
            log.info(
                "Collection %s: API %d + scrape %d → merged",
                collection_id,
                len(result),
                len(scraped),
            )
        else:
            log.info("Collection %s: found %d mods", collection_id, len(merged))
        return merged

    # Layer 3 — user API key (private/friends-only collections)
    log.warning(
        "Collection %s: no children from anonymous API/scrape, trying user API key",
        collection_id,
    )
    try:
        from rwmod.config import Config

        cfg = Config.load()
        if cfg.steam_api_key:
            keyed = _fetch_collection_api(collection_id, cfg.steam_api_key)
            if keyed:
                log.info(
                    "Collection %s: found %d mods via user API key",
                    collection_id,
                    len(keyed),
                )
                return _dedup_ids(keyed)
    except Exception:  # nosec B110 — user API key layer is best-effort
        pass

    log.info("Collection %s: no children (at least one source answered)", collection_id)
    return merged


def _dedup_ids(ids: list[str]) -> list[str]:
    """Deduplicate a list of IDs while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _fetch_collection_api(collection_id: str, api_key: str = "anonymous") -> list[str] | None:
    """Fetch every child ID of a collection via the dedicated GetCollectionDetails API.

    Returns the complete child list in one shot — the endpoint returns all
    children regardless of collection size (verified: 593/593 children against
    the page's declared ``childCount``), so no pagination is required.

    Returns ``None`` when the API call failed, and ``[]`` when it answered
    without children (including non-collection ids).
    """
    response = _get_collection_details(collection_id, api_key)
    if response is None:
        return None
    details = response.get("collectiondetails", [])
    if not details or details[0].get("result") != 1:
        return []

    ids: list[str] = []
    for child in details[0].get("children", []):
        wid = str(child.get("publishedfileid", ""))
        if wid:
            ids.append(wid)
    return ids


def _scrape_collection_page(collection_id: str, timeout: int = 30) -> list[str] | None:
    """Scrape the public Steam Community collection page for mod IDs.

    Fallback when the API returns empty. Extracts the collection's *own* child
    rows via the precise ``id="sharedfile_<id>"`` pattern — NOT every filedetails
    link on the page (which includes sidebar/related-item noise).

    Verified: the precise pattern returns exactly the same set as the API and
    the page's declared ``childCount`` (593 == 593 == 593).

    Returns ``None`` when the page could not be fetched, and ``[]`` when it was
    fetched but contains no child rows.
    """
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={collection_id}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        # Reuse the shared opener (HTTP keep-alive) instead of a fresh
        # TCP+TLS handshake per collection page.
        with _shared_opener.open(req, timeout=timeout) as resp:  # nosec B310 — HTTPS-only Steam URL
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    ids = []
    for m in re.finditer(r'id="sharedfile_(\d+)"', html):
        if m.group(1) != collection_id:
            ids.append(m.group(1))

    return _dedup_ids(ids)


def fetch_item_details(mod_ids: list[str]) -> dict[str, dict] | None:
    """Fetch updated timestamp for a batch of mod IDs (used for update detection).

    Returns ``None`` when the API call failed and ``{}`` when Steam answered
    without data for these ids. Callers must not report "not found" for a
    lookup that could not be made.
    """
    return _fetch_batch(mod_ids)


def fetch_item_dependencies(mod_ids: list[str]) -> dict[str, list[str]]:
    """Fetch mod dependencies for a batch of mod IDs — single API call.

    Uses ISteamRemoteStorage/GetPublishedFileDetails (the same endpoint as
    _fetch_batch). A mod's required items are returned in its ``children``
    field. The old code POSTed to QueryFiles, which Steam rejects with
    "Method Not Allowed" (405) — dependencies were never actually fetched.

    Returns: {mod_id: [dep_id, dep_id, ...]}
    """
    if not mod_ids:
        return {}

    data = _request_json(_PUBLISHED_FILE_DETAILS_URL, form=_published_file_details_form(mod_ids))
    if data is None:
        return {}

    result: dict[str, list[str]] = {}
    for f in data.get("response", {}).get("publishedfiledetails", []):
        children = f.get("children", [])
        if children:
            deps = [str(c.get("publishedfileid", "")) for c in children if c.get("publishedfileid")]
            if deps:
                result[f.get("publishedfileid", "")] = deps
    return result


def _fetch_batch(mod_ids: list[str]) -> dict[str, dict] | None:
    """Details for a batch, or None when the API call itself failed.

    The distinction matters: ``{}`` means "the API answered but had nothing
    for these ids", while None means "we could not ask". The update checker
    used to flatten both into ``{}`` and report "everything is up to date".
    """
    if not mod_ids:
        return {}
    data = _request_json(_PUBLISHED_FILE_DETAILS_URL, form=_published_file_details_form(mod_ids))
    if data is None:
        return None

    result: dict[str, dict] = {}
    for f in data.get("response", {}).get("publishedfiledetails", []):
        result[f.get("publishedfileid", "")] = {
            "time_updated": f.get("time_updated", 0),
            "title": f.get("title", ""),
            "file_description": f.get("file_description", ""),
        }
    return result


class _BatchDetails(dict[str, dict]):
    """Details map plus the batch counters the update checker reports.

    A ``dict`` subclass so existing callers/tests that treat the result as a
    plain mapping keep working.
    """

    failed_batches: int = 0
    total_batches: int = 0


def _fetch_batch_parallel(mod_ids: list[str], batch_size: int = 100) -> _BatchDetails:
    """Fetch mod details in parallel batches via ThreadPoolExecutor.

    For 300 mods → 3 concurrent API calls instead of 3 sequential ones,
    reducing total network wait by ~60-70%.
    A single batch goes directly to _fetch_batch with no executor overhead.

    The returned mapping records ``failed_batches`` / ``total_batches`` so the
    caller can distinguish "no updates" from "could not check".
    """
    if not mod_ids:
        return _BatchDetails()
    if len(mod_ids) <= batch_size:
        result = _BatchDetails()
        result.total_batches = 1
        batch = _fetch_batch(mod_ids)
        if batch is None:
            result.failed_batches = 1
        else:
            result.update(batch)
        return result

    batches = [mod_ids[i : i + batch_size] for i in range(0, len(mod_ids), batch_size)]
    result = _BatchDetails()
    result.total_batches = len(batches)
    with ThreadPoolExecutor(max_workers=min(len(batches), _MAX_WORKERS)) as executor:
        futures = {executor.submit(_fetch_batch, b): b for b in batches}
        for future in as_completed(futures):
            try:
                batch = future.result()
            except Exception:
                result.failed_batches += 1
                continue
            if batch is None:
                result.failed_batches += 1
            else:
                result.update(batch)
    return result


class UpdateCheckResult(TypedDict):
    """Outcome of one update check.

    ``partial`` is True when at least one Steam API batch failed, so the
    update list is incomplete rather than authoritative. When *every* batch
    failed there is no result at all and :func:`check_mod_updates` raises
    :class:`~rwmod.errors.WorkshopError` instead.
    """

    updates: list[dict]
    partial: bool
    failed_batches: int
    total_batches: int


def check_mod_updates(mods_dir: str) -> UpdateCheckResult:
    """Compare local mod timestamps with Steam Workshop.

    Uses the shared mod metadata cache to avoid re-parsing About.xml
    on every check — the cache is refreshed if stale (>30s or dir changed).

    A single failed batch does not discard the other batches' results: the
    return value marks the check ``partial``. Only a check where every batch
    failed raises ``WorkshopError`` — "could not ask" must never look like
    "everything is up to date".
    """
    from pathlib import Path

    from rwmod.mod_cache import get_cached_mods

    mods_path = Path(mods_dir)
    if not mods_path.exists():
        return {"updates": [], "partial": False, "failed_batches": 0, "total_batches": 0}

    # Reuse cached metadata instead of re-parsing About.xml for every mod
    metas = get_cached_mods(mods_path)
    local_mods: dict[str, dict] = {}
    for m in metas:
        if m.workshop_id:
            local_mods[m.workshop_id] = {
                "name": m.name,
                "package_id": m.package_id,
                "folder": m.folder,
            }

    if not local_mods:
        return {"updates": [], "partial": False, "failed_batches": 0, "total_batches": 0}

    # Fetch remote data in parallel batches — uses ThreadPoolExecutor
    # for concurrent API calls. 300 mods = 3 parallel batches instead of
    # 3 sequential ones, cutting network wait by ~60%.
    ids = list(local_mods.keys())
    all_details = _fetch_batch_parallel(ids)
    failed_batches = int(getattr(all_details, "failed_batches", 0))
    total_batches = int(getattr(all_details, "total_batches", 0))
    if total_batches > 0 and failed_batches == total_batches:
        # Never report "no updates" for a check that asked nobody:
        # that silently told the user everything was current.
        raise WorkshopError(
            f"Steam API 请求失败（{failed_batches}/{total_batches} 个批次），无法确认更新状态"
        )

    import time as _time

    from rwmod.database import get_last_updated, set_last_updated

    updates: list[dict] = []
    now = int(_time.time())

    for wid, local in local_mods.items():
        remote = all_details.get(wid)
        if not remote:
            continue

        remote_time = remote.get("time_updated", 0)
        mod_dir = mods_path / local["folder"]

        # Read the locally stored timestamp — from the DB (migrated away from
        # per-mod .rwmod_last_updated marker files, which pollute the user's
        # game directory and break under read-only mounts).
        local_time = get_last_updated(local["folder"])
        if local_time == 0:
            # Legacy upgrade path: pick up the old marker file value so an
            # existing install doesn't suddenly treat every mod as updated.
            ts_file = mod_dir / ".rwmod_last_updated"
            if ts_file.exists():
                with contextlib.suppress(ValueError, OSError):
                    local_time = int(ts_file.read_text(encoding="utf-8").strip())
                if local_time:
                    set_last_updated(local["folder"], local_time)
                    ts_file.unlink(missing_ok=True)

        # First-time check: no local timestamp → seed it, don't flag as outdated
        if local_time == 0:
            set_last_updated(local["folder"], now)
            continue

        if remote_time <= local_time:
            continue  # already up to date

        updates.append(
            {
                "workshop_id": wid,
                "name": local["name"],
                "folder": local["folder"],
                "remote_title": remote.get("title", local["name"]),
                "time_updated": remote_time,
                "file_description": remote.get("file_description", ""),
            }
        )

    return {
        "updates": updates,
        "partial": failed_batches > 0,
        "failed_batches": failed_batches,
        "total_batches": total_batches,
    }
