"""Shared XML parsing utilities — avoid duplicate About.xml parsing."""

from __future__ import annotations

from pathlib import Path

from rwmod.xmlutil import parse_xml_root

# Safe XML parser: rejects entity-expansion / external-entity (XXE) attacks.

__all__ = [
    "read_mod_metadata",
    "ModMeta",
    "installed_mod_metas",
    "installed_package_ids",
    "package_id_to_workshop_id",
]


class ModMeta:
    """Parsed metadata from a mod's About.xml."""

    __slots__ = ("folder", "name", "package_id", "workshop_id", "supported_versions")

    def __init__(
        self,
        folder: str,
        name: str = "?",
        package_id: str = "",
        workshop_id: str = "",
        supported_versions: list[str] | None = None,
    ):
        self.folder = folder
        self.name = name
        self.package_id = package_id
        self.workshop_id = workshop_id
        self.supported_versions = supported_versions or []


def read_mod_metadata(mod_dir: Path) -> ModMeta | None:
    """Read About.xml + PublishedFileId.txt from a mod directory.
    Returns None if the directory doesn't look like a valid mod.
    """
    if not mod_dir.is_dir():
        return None

    about = mod_dir / "About" / "About.xml"
    if not about.exists():
        return None

    name = "?"
    pkg = ""
    versions: list[str] = []
    try:
        root = parse_xml_root(about)
        name = root.findtext("name", "?") or "?"
        pkg = root.findtext("packageId", "") or ""
        sv = root.find("supportedVersions")
        if sv is not None:
            versions = [li.text or "" for li in sv.findall("li") if li.text]
    except Exception:
        pass

    pf = mod_dir / "About" / "PublishedFileId.txt"
    wid = ""
    if pf.exists():
        try:
            wid = pf.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            wid = ""
    # Only numeric workshop IDs are trusted — a malicious or corrupted
    # PublishedFileId.txt (e.g. containing "../x" or SteamCMD command tokens)
    # must never flow into backup filenames or SteamCMD arguments.
    if wid and not wid.isdigit():
        wid = ""

    return ModMeta(
        folder=mod_dir.name, name=name, package_id=pkg, workshop_id=wid, supported_versions=versions
    )


def installed_mod_metas(mods_dir: Path) -> list[ModMeta]:
    """Read metadata for every valid mod in mods_dir, in directory-name order.

    A missing mods_dir yields [] instead of raising: every caller treats "no
    mods" and "no mods_dir" the same way, and three of them used to crash here.

    Order is directory name, which callers that emit a load order rely on.
    """
    if not mods_dir.is_dir():
        return []
    metas: list[ModMeta] = []
    for d in sorted(mods_dir.iterdir()):
        meta = read_mod_metadata(d)
        if meta is not None and meta.package_id:
            metas.append(meta)
    return metas


def installed_package_ids(mods_dir: Path) -> set[str]:
    """packageId of every installed mod."""
    return {m.package_id for m in installed_mod_metas(mods_dir)}


def package_id_to_workshop_id(mods_dir: Path) -> dict[str, str]:
    """Map packageId → workshop ID for mods that declare both.

    Built on read_mod_metadata, so the "numeric workshop IDs only" guard applies
    to every caller. Three hand-rolled copies of this map used to exist and only
    one of them checked the ID; sharing the reader is what keeps the guard from
    drifting away again.
    """
    return {m.package_id: m.workshop_id for m in installed_mod_metas(mods_dir) if m.workshop_id}
