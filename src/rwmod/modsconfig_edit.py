"""Batch enable/disable of mods in RimWorld's ``ModsConfig.xml``.

The planning functions are pure (no disk access) so the enable/disable rules
can be tested without a sandbox. Only the ``<activeMods>`` child is rewritten:
the surrounding document — ``version``, ``knownExpansions``, comments and any
other node — is preserved byte-for-byte, including CRLF line endings and an
existing UTF-8 BOM.

Writing goes through :func:`write_modsconfig_target`, which takes a target that
the caller obtained from :func:`rwmod.profile.modsconfig_target` (the only
sandbox-guarded ModsConfig location) and performs the same three-step safety
dance as the other writers: timestamped ``.bak_*`` copy, undo snapshot, then
an atomic ``os.replace``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from rwmod.errors import ValidationError
from rwmod.mod_rules import is_official_package_id, normalize_package_id, split_anchor_order

__all__ = [
    "ActiveModsPlan",
    "active_order_needs_sort",
    "plan_active_changes",
    "read_active_mods",
    "read_modsconfig_text",
    "rewrite_active_mods",
    "write_modsconfig_target",
]

_log = logging.getLogger(__name__)

# Only the first group boundaries matter: rewrite replaces group 2 (the inner
# content) and leaves the start/end tags exactly as RimWorld wrote them.
_ACTIVE_RE = re.compile(r"(<activeMods\b[^>]*>)(.*?)(</activeMods\s*>)", re.DOTALL)
_ACTIVE_EMPTY_RE = re.compile(r"<activeMods\b[^>]*/>")
_ITEM_INDENT_RE = re.compile(r"(?m)^([ \t]*)<li\b")

#: ``Core`` is not a packageId in 1.6 (``ludeon.rimworld`` is), but old
#: ModsConfig files and users still refer to it by folder name.
_CORE_ALIAS = "core"


@dataclass(frozen=True)
class ActiveModsPlan:
    """Result of planning a batch enable/disable.

    ``active`` keeps the caller's spelling and the existing order; ``added``
    and ``removed`` report what actually changed (so a no-op request answers
    with empty lists and a populated ``skipped``). ``needs_sort`` is True when
    the resulting order violates the Harmony → Core → DLC anchor order.
    """

    active: list[str]
    added: list[str]
    removed: list[str]
    skipped: list[str]
    needs_sort: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "added": self.added,
            "removed": self.removed,
            "skipped": self.skipped,
            "needs_sort": self.needs_sort,
        }


# ── read ───────────────────────────────────────────────────────────


def read_modsconfig_text(path: Path) -> str:
    """Read ModsConfig.xml as UTF-8 text without normalising line endings.

    Decoding as plain ``utf-8`` (not ``utf-8-sig``) keeps a leading BOM as a
    U+FEFF character, so :func:`rewrite_active_mods` can write it back
    unchanged. RimWorld writes UTF-8; a file that is not decodable is reported
    as invalid instead of being silently mangled.
    """
    try:
        return Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ValidationError(f"读取 ModsConfig.xml 失败: {e}") from e


def read_active_mods(path: Path) -> list[str]:
    """Return ``<activeMods><li>`` ids in file order (original case preserved).

    Raises :class:`~rwmod.errors.ValidationError` when the file is not
    well-formed or has no ``activeMods`` element — callers translate that into
    the API's 400 failure shape.
    """
    text = read_modsconfig_text(path)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValidationError(f"解析 ModsConfig.xml 失败: {e}") from e

    active = root.find("activeMods")
    if active is None:
        raise ValidationError("ModsConfig.xml 中没有 activeMods")
    ids: list[str] = []
    for li in active.findall("li"):
        pid = (li.text or "").strip()
        if pid:
            ids.append(pid)
    return ids


# ── planning (pure) ────────────────────────────────────────────────


def plan_active_changes(
    current: Sequence[str],
    *,
    enable: Sequence[str] = (),
    disable: Sequence[str] = (),
    installed: Sequence[str] = (),
) -> ActiveModsPlan:
    """Plan a batch enable/disable without touching the filesystem.

    Rules (all comparisons case-insensitive via ``normalize_package_id``):

    * an id in *enable* is accepted when it is an installed packageId, Core/DLC
      (``is_official_package_id`` / the ``Core`` alias), or already active —
      appending happens at the end and never reorders existing entries;
    * an id in *disable* is accepted only when it is currently active;
    * an id the request cannot act on — unknown, already active, no longer
      active, or listed in both lists — is reported in ``skipped``;
    * enabling a duplicate of an active id is a no-op, not a duplicate entry.

    The caller's spelling is preserved for written ids; matching is always
    case-insensitive.
    """
    active = [pid.strip() for pid in current if pid and pid.strip()]
    seen = {normalize_package_id(pid) for pid in active if normalize_package_id(pid)}

    enable_ids = [pid.strip() for pid in enable if pid and pid.strip()]
    disable_ids = [pid.strip() for pid in disable if pid and pid.strip()]
    enable_norms = {normalize_package_id(pid) for pid in enable_ids}
    disable_norms = {normalize_package_id(pid) for pid in disable_ids}
    conflicting = enable_norms & disable_norms

    installed_norms = {normalize_package_id(pid) for pid in installed if pid}
    installed_norms.discard("")

    skipped: list[str] = []
    removed: list[str] = []

    for raw in disable_ids:
        norm = normalize_package_id(raw)
        if norm in conflicting or norm not in seen:
            skipped.append(raw)
            continue
        spelling = next((pid for pid in active if normalize_package_id(pid) == norm), raw)
        active = [pid for pid in active if normalize_package_id(pid) != norm]
        seen.discard(norm)
        removed.append(spelling)

    added: list[str] = []
    for raw in enable_ids:
        norm = normalize_package_id(raw)
        if norm in conflicting or norm in seen:
            skipped.append(raw)
            continue
        if not (norm in installed_norms or _is_official(norm)):
            skipped.append(raw)
            continue
        active.append(raw)
        seen.add(norm)
        added.append(raw)

    return ActiveModsPlan(
        active=active,
        added=added,
        removed=removed,
        skipped=skipped,
        needs_sort=active_order_needs_sort(active),
    )


def active_order_needs_sort(ids: Sequence[str]) -> bool:
    """True when *ids* violates the Harmony → Core → DLC anchor order.

    Duplicate ids also count as "needs sorting": RimWorld treats a duplicated
    packageId as the same mod and the sorted list is de-duplicated.
    """
    clean = [pid for pid in ids if normalize_package_id(pid)]
    anchored, rest = split_anchor_order(clean)
    return [*anchored, *rest] != clean


def _is_official(norm: str) -> bool:
    return norm == _CORE_ALIAS or is_official_package_id(norm)


# ── rewrite (pure) ─────────────────────────────────────────────────


def rewrite_active_mods(xml_text: str, active_ids: Sequence[str]) -> str:
    """Return *xml_text* with only the ``<activeMods>`` inner content replaced.

    Everything outside the element — XML declaration, ``version``,
    ``knownExpansions``, whitespace, comments — is preserved verbatim. Line
    endings are copied from the document (CRLF stays CRLF, LF stays LF) and a
    leading BOM (kept as U+FEFF by :func:`read_modsconfig_text`) survives.

    Raises :class:`~rwmod.errors.ValidationError` unless exactly one
    ``activeMods`` element exists. A self-closing ``<activeMods/>`` is expanded
    when ids are added and left as an explicit empty pair otherwise.
    """
    matches = list(_ACTIVE_RE.finditer(xml_text))
    empty_matches = list(_ACTIVE_EMPTY_RE.finditer(xml_text))
    if len(matches) + len(empty_matches) != 1:
        raise ValidationError("ModsConfig.xml 必须恰好包含一个 activeMods 节点")

    newline = "\r\n" if "\r\n" in xml_text else "\n"

    if matches:
        match = matches[0]
        base_indent = _line_indent(xml_text, match.start())
        item_indent = _item_indent(match.group(2), base_indent)
        inner = _inner_block(active_ids, newline, item_indent, base_indent)
        # Replace only group 2: the start/end tags keep their original
        # attributes and spelling.
        return xml_text[: match.start(2)] + inner + xml_text[match.end(2) :]

    # Self-closing form: <activeMods /> / <activeMods/>.
    empty = empty_matches[0]
    tag = empty.group(0)
    open_tag = tag[:-2].rstrip() + ">"
    base_indent = _line_indent(xml_text, empty.start())
    item_indent = f"{base_indent}  "
    inner = _inner_block(active_ids, newline, item_indent, base_indent)
    return xml_text[: empty.start()] + open_tag + inner + "</activeMods>" + xml_text[empty.end() :]


def _inner_block(
    active_ids: Sequence[str], newline: str, item_indent: str, base_indent: str
) -> str:
    """Build the replacement content between the ``activeMods`` tags."""
    if not active_ids:
        return ""
    items = "".join(f"{item_indent}<li>{xml_escape(pid)}</li>{newline}" for pid in active_ids)
    return f"{newline}{items}{base_indent}"


def _line_indent(text: str, pos: int) -> str:
    """Whitespace at the start of the line containing *pos*."""
    line_start = text.rfind("\n", 0, pos) + 1
    line = text[line_start:pos]
    return line[: len(line) - len(line.lstrip(" \t"))]


def _item_indent(old_inner: str, base_indent: str) -> str:
    """Indentation for ``<li>`` rows, copied from the existing content."""
    match = _ITEM_INDENT_RE.search(old_inner)
    if match:
        return match.group(1)
    return f"{base_indent}  "


# ── atomic writer ──────────────────────────────────────────────────

# Serializes this process's writers. Unique temp names keep two processes
# from sharing a scratch file; within one process the backup/snapshot read
# and the replace still must not interleave — on Windows, replacing a file
# another thread has open for the backup copy fails with PermissionError.
_WRITE_LOCK = threading.Lock()


def write_modsconfig_target(
    target: Path, text: str | bytes, *, label: str = "batch-active"
) -> Path | None:
    """Write *text* to *target* after backing it up and snapshotting it.

    *text* may be ``str`` (encoded as UTF-8, unchanged behaviour) or raw
    ``bytes`` (written verbatim, so a caller that preserved a BOM keeps it
    byte-for-byte instead of round-tripping it through a Python string).

    *target* must be the path returned by
    :func:`rwmod.profile.modsconfig_target` — the sandbox guard is re-applied
    here as defence in depth, so a caller that assembled the path by hand still
    fails closed under ``RWMOD_TEST_SANDBOX_HOME``.

    Returns the ``.bak_<ts>`` path, or None when the target did not exist yet.
    """
    target = Path(target)
    _assert_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    with _WRITE_LOCK:
        backup: Path | None = None
        if target.exists():
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
            backup = target.with_name(f"{target.name}.bak_{ts}")
            counter = 1
            while backup.exists():
                backup = target.with_name(f"{target.name}.bak_{ts}_{counter}")
                counter += 1
            shutil.copy2(target, backup)

            from rwmod.undo import snapshot_modsconfig

            snapshot_modsconfig(target, label=label)

        # A unique temp name per writer. A fixed "<target>.rwmod.tmp" made two
        # concurrent read-modify-writes share one scratch file: one writer's
        # os.replace() moved the other's half-written content into place (lost
        # update) or the loser's replace failed with FileNotFoundError (500).
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{target.name}.", suffix=".rwmod.tmp", dir=target.parent
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(text if isinstance(text, bytes) else text.encode("utf-8"))
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

        _log.info("写入 ModsConfig.xml: %s（备份 %s）", target, backup.name if backup else "无")
        return backup


def _assert_target(target: Path) -> None:
    """Reject anything that is not a sandbox-checked ``ModsConfig.xml``."""
    if target.name != "ModsConfig.xml":
        raise ValidationError(f"只允许写入 ModsConfig.xml，而不是 {target.name!r}")

    # modsconfig_target() runs profile._assert_within_sandbox() on
    # <parent>/ModsConfig.xml, i.e. exactly this target. Keeping the call here
    # means every writer inherits the Wave-1 guard without duplicating it.
    from rwmod.profile import modsconfig_target

    modsconfig_target(target.parent, config_dir=target.parent)
