from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

"""Shared utilities - mod ID extraction, safe filenames, and common helpers.

All modules that need extract_mod_id or safe_filename should import from here
instead of duplicating the logic across backup.py, profile.py, and downloader.py.
"""

__all__ = ["extract_mod_id", "safe_filename", "safe_extract_zip", "bundle_root"]


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


def safe_extract_zip(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    """Extract a zip archive safely.

    Rejects members with absolute paths or ``..`` traversal so a malicious
    archive can never write outside ``dest_dir``. Raises zipfile.BadZipFile
    on unsafe content (nothing is extracted in that case).

    Args:
        zf: Open zip archive (read mode).
        dest_dir: Directory to extract into.
    """
    dest_resolved = dest_dir.resolve()
    for member in zf.infolist():
        name = member.filename.replace("\\", "/")
        unsafe = name.startswith("/") or name.startswith("../") or "/../" in name
        if unsafe or name in ("", ".", ".."):
            raise zipfile.BadZipFile(f"zip 包含不安全的路径: {member.filename!r}")
        target = (dest_dir / member.filename).resolve()
        if not target.is_relative_to(dest_resolved):
            raise zipfile.BadZipFile(f"zip 包含越界路径: {member.filename!r}")
    zf.extractall(dest_dir)
