"""RimWorld save file (.rws) parser \u2014 extract mod requirements from save games.

A RimWorld save file is XML-based and contains a <modIds> section listing all
packageIds active when the save was created. This module extracts that list
along with the game version.
"""

from __future__ import annotations

import mmap
import re
from pathlib import Path
from typing import Any

from rwmod.metadata import package_id_key

__all__ = ["parse_save_mods", "analyze_save", "find_save_files"]

# Pre-compiled regex for skipping full XML parse (saves can be 50+ MB).
# The byte variants scan a memory-mapped file: the <modIds> section can sit
# anywhere in the file, and a fixed-size text window silently lost it once it
# crossed the old 2 MB boundary (PARSE-6). mmap maps without copying the
# whole save into a Python string.
_MODIDS_RE = re.compile(r"<modIds>(.*?)</modIds>", re.DOTALL)
_LI_RE = re.compile(r"<li>([^<]+)</li>")
_MODIDS_RE_BYTES = re.compile(rb"<modIds>(.*?)</modIds>", re.DOTALL)
_LI_RE_BYTES = re.compile(rb"<li>([^<]+)</li>")


def find_save_files(rimworld_dir: Path, config_dir: Path | None = None) -> list[Path]:
    """Find all .rws save files across common locations.

    ``config_dir`` (the active instance's explicit config directory) takes
    precedence: its ``Saves/`` folder is scanned and the global LocalLow tree
    is never touched, so two instances do not see each other's colonies.
    Without it, the historical LocalLow discovery applies.
    """
    import platform

    saves: list[Path] = []
    if config_dir is not None:
        instance_saves = Path(config_dir) / "Saves"
        if instance_saves.exists():
            saves.extend(instance_saves.rglob("*.rws"))
    else:
        if platform.system() == "Windows":
            base = Path.home() / "AppData" / "LocalLow" / "Ludeon Studios"
        elif platform.system() == "Darwin":
            base = Path.home() / "Library" / "Application Support" / "Ludeon Studios"
        else:
            base = Path.home() / ".config" / "unity3d" / "Ludeon Studios"
        for save_config_dir in base.glob("RimWorld*"):
            saves_dir = save_config_dir / "Saves"
            if saves_dir.exists():
                saves.extend(saves_dir.rglob("*.rws"))
    local_saves = rimworld_dir / "Saves"
    if local_saves.exists():
        saves.extend(local_saves.rglob("*.rws"))
    return sorted(set(saves), key=lambda p: p.stat().st_mtime, reverse=True)


def parse_save_mods(content_or_path: str | Path) -> list[str]:
    """Extract mod packageIds from a .rws save file.

    Uses regex-based extraction to avoid loading the entire XML tree.
    Accepts either a Path or raw XML content string.
    """
    if isinstance(content_or_path, Path):
        path = content_or_path
        if not path.exists():
            return []
        try:
            if path.stat().st_size == 0:
                return []
            with (
                open(path, "rb") as f,
                mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mapped,
            ):
                m = _MODIDS_RE_BYTES.search(mapped)
                if m:
                    return [
                        item.decode("utf-8", errors="replace")
                        for item in _LI_RE_BYTES.findall(m.group(1))
                    ]
                return []
        except (OSError, ValueError):
            # ValueError: e.g. an empty/racing file that cannot be mapped.
            return []

    text_match = _MODIDS_RE.search(content_or_path)
    if text_match:
        return _LI_RE.findall(text_match.group(1))
    return []


def analyze_save(
    save_path: Path,
    installed_package_ids: set[str],
) -> dict[str, Any]:
    """Analyze a save file against installed mods.

    Returns:
        {
            "file": str, "name": str, "game_version": str | None,
            "total_mods": int, "required_mods": [str],
            "missing_mods": [str], "unused_by_save": [str],
            "completeness": float, "loadable": bool,
        }
    """
    required = parse_save_mods(save_path)
    game_version = _extract_game_version(save_path)
    name = save_path.stem
    # RimWorld writes lowercase ids into saves, About.xml keeps the author's
    # casing. Compare through the normalized key so the two match; every value
    # returned to the caller keeps the spelling from its original source.
    installed_keys = {package_id_key(pkg) for pkg in installed_package_ids}
    required_keys = {package_id_key(pkg) for pkg in required}
    missing = [pkg for pkg in required if package_id_key(pkg) not in installed_keys]
    unused = [pkg for pkg in installed_package_ids if package_id_key(pkg) not in required_keys]
    completeness = len(required) / (len(required) + len(missing)) if required else 1.0
    return {
        "file": str(save_path),
        "name": name,
        "game_version": game_version,
        "total_mods": len(required),
        "required_mods": required,
        "missing_mods": missing,
        "unused_by_save": unused,
        "completeness": round(completeness, 3),
        "loadable": len(missing) == 0,
    }


def _extract_game_version(save_path: Path) -> str | None:
    """Try to extract game version from save file header."""
    try:
        with open(save_path, encoding="utf-8", errors="replace") as f:
            head = f.read(500_000)
        m = re.search(r"<gameVersion>(.*?)</gameVersion>", head)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return None
