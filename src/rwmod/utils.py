from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import UploadFile

"""Shared utilities - mod ID extraction, safe filenames, and common helpers.

All modules that need extract_mod_id or safe_filename should import from here
instead of duplicating the logic across backup.py, profile.py, and downloader.py.
"""

__all__ = [
    "extract_mod_id",
    "safe_filename",
    "safe_extract_zip",
    "bundle_root",
    "read_upload_limited",
]


def bundle_root() -> Path:
    """Return the directory that holds bundled static/steamcmd resources.

    Works both from a source checkout (repo root) and from a PyInstaller
    one-file build (the sys._MEIPASS extraction directory). In a frozen
    one-file EXE, ``__file__`` points into a temp dir whose parent chain does
    NOT contain the resources, so ``Path(__file__).parent...`` must not be used.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent


def extract_mod_id(raw: str) -> str:
    """Extract a numeric workshop/mod ID from a URL or raw string.

    Handles:
        - Raw IDs: "2009463077"
        - Steam URLs: "https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077"
        - Workshop URLs: "https://steamcommunity.com/workshop/filedetails/?id=2009463077"

    Returns the numeric ID string, or empty string if not parseable.
    """
    raw = raw.strip()
    if not raw:
        return ""
    m = re.search(r"[?&]id=(\d+)", raw)
    if m:
        return m.group(1)
    if raw.isdigit():
        return raw
    return ""


def safe_filename(name: str, allow_empty: bool = False) -> str:
    r"""Replace filesystem-unfriendly characters in a name.

    Removes characters that are invalid in Windows/Unix filenames:
    < > : " / \ | ? *
    Also strips leading/trailing dots and spaces.

    Args:
        name: The raw name to sanitize.
        allow_empty: If False, returns "unnamed" when the result is empty
                     after sanitization. If True, returns the empty string.

    Returns:
        A safe filename string.
    """
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", name.strip()).strip("._")
    if not sanitized and not allow_empty:
        sanitized = "unnamed"
    return sanitized


# Decompression bomb guard: a malicious archive must not be able to exhaust
# disk / memory. Mod zips are typically < 1GB; 4GB is generous headroom.
MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB uncompressed total
MAX_ZIP_MEMBERS = 50_000


def safe_extract_zip(
    zf: zipfile.ZipFile,
    dest_dir: Path,
    max_total_bytes: int = MAX_ZIP_TOTAL_BYTES,
    max_members: int = MAX_ZIP_MEMBERS,
) -> None:
    """Extract a zip archive safely.

    Rejects members with absolute paths or ``..`` traversal so a malicious
    archive can never write outside ``dest_dir``. Also rejects zip bombs
    (excessive total uncompressed size / member count) BEFORE extracting
    anything. Raises zipfile.BadZipFile on unsafe content.

    Args:
        zf: Open zip archive (read mode).
        dest_dir: Directory to extract into.
        max_total_bytes: Cap on the sum of uncompressed member sizes.
        max_members: Cap on the number of archive members.
    """
    dest_resolved = dest_dir.resolve()
    total = 0
    members = zf.infolist()
    if len(members) > max_members:
        raise zipfile.BadZipFile(f"zip 成员过多（{len(members)} > {max_members}）")
    seen: set[str] = set()
    for member in members:
        name = member.filename.replace("\\", "/")
        unsafe = name.startswith("/") or name.startswith("../") or "/../" in name
        if unsafe or name in ("", ".", ".."):
            raise zipfile.BadZipFile(f"zip 包含不安全的路径: {member.filename!r}")
        target = (dest_dir / member.filename).resolve()
        if not target.is_relative_to(dest_resolved):
            raise zipfile.BadZipFile(f"zip 包含越界路径: {member.filename!r}")
        if name in seen:
            raise zipfile.BadZipFile(f"zip 包含重复路径: {member.filename!r}")
        seen.add(name)
        total += member.file_size
        if total > max_total_bytes:
            raise zipfile.BadZipFile("zip 解压总量超限（疑似压缩炸弹）")
    zf.extractall(dest_dir)


async def read_upload_limited(file: UploadFile, max_bytes: int) -> bytes | None:
    """Read an async upload with a hard byte cap.

    ``UploadFile.size`` is only set when the client sends Content-Length; a
    chunked upload has ``size is None``, so a ``if file.size > cap`` guard is
    silently skipped and ``await file.read()`` buffers the whole body into
    memory. Reading ``max_bytes + 1`` bytes instead makes the cap independent
    of the client's framing: if we get back more than max_bytes the upload is
    rejected (returning None) without ever buffering more than cap+1 bytes.

    Args:
        file: An async file-like object (FastAPI UploadFile).
        max_bytes: Hard cap on the accepted body size.

    Returns:
        The body bytes, or None if the upload exceeds ``max_bytes``.
    """
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        return None
    return data
