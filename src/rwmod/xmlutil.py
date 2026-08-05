"""Safe XML helpers built on defusedxml.

All untrusted XML (mod metadata, uploaded modlists, About.xml) must be parsed
through this module so entity-expansion / external-entity (XXE) attacks are
rejected by defusedxml instead of the stdlib ElementTree.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET

_log = logging.getLogger(__name__)

__all__ = ["parse_about_field", "parse_xml_root"]


def parse_xml_root(path: Path) -> Element:
    """Parse an XML file safely and return its root element.

    Raises the same exceptions as ElementTree.parse (ETParseError, OSError).
    """
    root = ET.parse(path).getroot()
    # defusedxml's stubs type getroot() as optional; a well-formed parsed
    # document always has a root — collapse the None branch here so the
    # dozens of call sites don't each need a defensive check.
    return cast(Element, root)


def parse_about_field(about_path: Path, tag: str) -> str:
    """Extract a field (e.g. packageId, name) from a RimWorld About.xml.

    Returns "" on any parse/read error so callers can degrade gracefully.
    """
    try:
        root = parse_xml_root(about_path)
        if root is None:
            return ""
        value = root.findtext(tag, "")
        return value.strip() if value else ""
    except Exception:  # noqa: BLE001 — About.xml may be malformed; never crash
        _log.debug("解析 %s 的 %s 字段失败", about_path.name, tag)
        return ""
