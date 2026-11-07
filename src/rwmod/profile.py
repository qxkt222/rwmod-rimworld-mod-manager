"""Mod profile management — save/restore ModsConfig.xml snapshots.

RimWorld players often switch between mod sets (vanilla, medieval, etc.).
Profiles are stored as named XML copies in ``~/.rwmod/profiles/<instance_id>/``
so two RimWorld installs (instances) keep independent profile lists. Files in
the pre-multi-instance layout (``~/.rwmod/profiles/*.xml``) are migrated into
the ``default/`` instance directory on first use.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from rwmod.config import current_instance_id, is_valid_instance_id
from rwmod.utils import safe_filename
from rwmod.xmlutil import parse_xml_root

__all__ = [
    "PROFILES_DIR",
    "SANDBOX_ENV_VAR",
    "SandboxViolationError",
    "delete_profile",
    "list_profiles",
    "modsconfig_target",
    "profiles_dir_for",
    "resolve_modsconfig_path",
    "restore_profile",
    "save_profile",
]

PROFILES_DIR = Path.home() / ".rwmod" / "profiles"

#: Test-only marker. When set, every ModsConfig.xml path this module resolves
#: or writes must stay under this root; anything else raises instead of falling
#: back to the user's real LocalLow profile.
SANDBOX_ENV_VAR = "RWMOD_TEST_SANDBOX_HOME"

_log = logging.getLogger(__name__)


class SandboxViolationError(RuntimeError):
    """A ModsConfig.xml path escaped ``RWMOD_TEST_SANDBOX_HOME``.

    Deliberately a plain RuntimeError: this is a fail-closed safety guard, not
    a recoverable business error that an endpoint should turn into a 4xx.
    """


def _sandbox_root() -> Path | None:
    """Return the declared sandbox root, or None when no marker is set."""
    raw = os.environ.get(SANDBOX_ENV_VAR)
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError) as e:  # pragma: no cover — bad marker = config bug
        raise SandboxViolationError(f"无法解析 {SANDBOX_ENV_VAR}: {raw!r}") from e


def _assert_within_sandbox(path: Path) -> Path:
    """Return *path* unchanged, or raise if it resolves outside the sandbox.

    ``resolve()`` follows symlinks, so a link inside the sandbox that points at
    the real LocalLow still fails closed.
    """
    root = _sandbox_root()
    if root is None:
        return path
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        raise SandboxViolationError(f"无法解析 ModsConfig 路径: {path}") from e
    if not resolved.is_relative_to(root):
        raise SandboxViolationError(
            f"拒绝访问 {SANDBOX_ENV_VAR} 之外的 ModsConfig 路径: {resolved}"
        )
    return path


def resolve_modsconfig_path(
    rimworld_dir: Path | None = None, config_dir: Path | None = None
) -> Path | None:
    """Find the active ModsConfig.xml for the RimWorld installation.

    Args:
        rimworld_dir: RimWorld install directory (fallback location).
        config_dir: Explicit config directory. When given, only
            ``<config_dir>/ModsConfig.xml`` is considered and LocalLow is
            never scanned — this is how callers pin the file to a known
            location (and how the test sandbox stays inside tmp).

    Precedence when ``config_dir`` is None:
    1. Standard Ludeon Studios config (Steam, GoG)
    2. RimWorld install directory

    With ``RWMOD_TEST_SANDBOX_HOME`` set, any candidate that resolves outside
    that root raises :class:`SandboxViolationError` instead of being returned.
    """
    if config_dir is not None:
        explicit = _assert_within_sandbox(Path(config_dir) / "ModsConfig.xml")
        return explicit if explicit.exists() else None

    import platform

    # Location 1: Ludeon Studios app data (most common)
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "LocalLow" / "Ludeon Studios"
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Ludeon Studios"
    else:
        base = Path.home() / ".config" / "unity3d" / "Ludeon Studios"

    for candidate in base.glob("RimWorld*"):
        path = candidate / "Config" / "ModsConfig.xml"
        if path.exists():
            return _assert_within_sandbox(path)

    # Location 2: RimWorld install directory
    if rimworld_dir is not None:
        local = Path(rimworld_dir) / "ModsConfig.xml"
        if local.exists():
            return _assert_within_sandbox(local)

    return None


def modsconfig_target(rimworld_dir: Path, config_dir: Path | None = None) -> Path:
    """Return the ModsConfig.xml path a writer should use.

    Unlike :func:`resolve_modsconfig_path` this always returns a path, even
    when the file does not exist yet — the caller decides whether that means
    "create it" or "404". Both the explicit and fallback paths go through the
    sandbox guard.
    """
    if config_dir is not None:
        return _assert_within_sandbox(Path(config_dir) / "ModsConfig.xml")
    resolved = resolve_modsconfig_path(rimworld_dir)
    if resolved is not None:
        return resolved
    return _assert_within_sandbox(Path(rimworld_dir) / "ModsConfig.xml")


def profiles_dir_for(instance_id: str | None = None, *, create: bool = True) -> Path:
    """Return ``<PROFILES_DIR>/<instance_id>``, migrating legacy root files.

    ``create=False`` is used by read-only paths (export/restore/delete) so they
    never create a directory for an empty instance.
    """
    iid = instance_id or current_instance_id()
    if not is_valid_instance_id(iid):
        raise ValueError(f"非法实例 id: {iid!r}")
    base = Path(PROFILES_DIR)
    # Fail closed before any migration write when the base escaped the sandbox.
    _assert_within_sandbox(base)
    dest = base / iid
    _assert_within_sandbox(dest)
    _migrate_legacy_profiles(base)
    if create:
        dest.mkdir(parents=True, exist_ok=True)
    return dest


def _migrate_legacy_profiles(base: Path) -> None:
    """Move pre-multi-instance ``base/*.xml`` into ``base/default/`` exactly once."""
    if not base.is_dir():
        return
    legacy = [p for p in base.glob("*.xml") if p.is_file()]
    if not legacy:
        return
    dest_dir = base / "default"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in legacy:
        dest = dest_dir / src.name
        suffix = 2
        while dest.exists():
            dest = dest_dir / f"{src.stem}-legacy{suffix}{src.suffix}"
            suffix += 1
        try:
            os.replace(src, dest)
        except OSError:  # pragma: no cover — a racing third party moved it first
            continue


def list_profiles(instance_id: str | None = None) -> list[dict]:
    """List all saved profiles of one instance with metadata."""
    directory = profiles_dir_for(instance_id)
    results: list[dict] = []
    for f in sorted(directory.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        name = f.stem
        mod_count = _count_mods_in_xml(f)
        mtime = f.stat().st_mtime
        results.append(
            {
                "name": name,
                "mod_count": mod_count,
                "saved_at": datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
                "size_kb": round(f.stat().st_size / 1024, 1),
            }
        )
    return results


def _remove_legacy_temp(target: Path) -> None:
    """Delete the fixed ``<target>.rwmod.tmp`` scratch file used before REV-010.

    A crashed pre-fix writer could leave one behind; leaving it would look like
    live data to a curious user. Only a regular file is removed — a directory
    squatting on the name is left alone so the atomic write fails loudly
    instead of deleting something unexpected.
    """
    legacy = target.with_name(f"{target.name}.rwmod.tmp")
    try:
        if legacy.is_file():
            legacy.unlink(missing_ok=True)
    except OSError:  # best-effort cleanup must never fail the write
        pass


def save_profile(
    name: str, source_path: Path | str, label: str = "", instance_id: str | None = None
) -> dict:
    """Save a ModsConfig.xml snapshot as a named profile.

    Args:
        name: Profile name (used as filename stem).
        source_path: Path to ModsConfig.xml, or XML string content.
        label: Optional human-readable description.
        instance_id: Owning instance (defaults to the active one).

    Returns ``{"ok": bool, "msg": str}`` (plus ``mod_count`` on success). The
    routers translate ``ok: False`` into an ``errors.py`` exception; library
    callers (the CLI) keep the tuple-style result they have always used.
    """
    dest = profiles_dir_for(instance_id) / f"{safe_filename(name)}.xml"
    safe_name = dest.stem

    source = (
        Path(source_path)
        if isinstance(source_path, str) and not source_path.startswith("<")
        else source_path
    )

    try:
        if isinstance(source, Path) and source.exists():
            content = source.read_text(encoding="utf-8")
        else:
            content = str(source)
    except Exception as e:
        return {"ok": False, "msg": f"读取失败: {e}"}

    # Validate with the defused parser (a profile can be a hand-dropped file;
    # entity-expansion / XXE must be rejected, not fed to stdlib ElementTree).
    # Write a temp file first so a rejected/incomplete save never truncates an
    # existing profile of the same name; then swap it in atomically. The temp
    # name must be unique per writer: a fixed "<dest>.rwmod.tmp" let two
    # concurrent save requests of the same profile share one scratch file
    # (REV-010). Remove such a scratch file if a pre-fix writer crashed and
    # left one behind.
    _remove_legacy_temp(dest)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{dest.name}.", suffix=".rwmod.tmp", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        try:
            parse_xml_root(tmp)
        except Exception as e:
            return {"ok": False, "msg": f"无效的 XML: {e}"}
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)

    mod_count = _count_mods_in_xml(dest)
    _log.info("保存 profile: %s (%d mods)", safe_name, mod_count)
    return {
        "ok": True,
        "msg": f"已保存 '{safe_name}'（{mod_count} 个 Mod）",
        "mod_count": mod_count,
    }


def restore_profile(name: str, target_path: Path, instance_id: str | None = None) -> dict:
    """Restore a profile by copying it to the ModsConfig.xml location.

    Args:
        name: Profile name to restore.
        target_path: Where to write ModsConfig.xml.
        instance_id: Owning instance (defaults to the active one).

    Returns ``{"ok": bool, "msg": str}`` (plus ``mod_count`` on success); the
    router turns ``ok: False`` into the matching ``errors.py`` exception.
    """
    # Fail closed before touching anything: a target outside the declared test
    # sandbox raises (SandboxViolationError) instead of being written.
    _assert_within_sandbox(target_path)

    safe_name = safe_filename(name)
    src = profiles_dir_for(instance_id, create=False) / f"{safe_name}.xml"

    if not src.exists():
        return {"ok": False, "msg": f"Profile 不存在: {safe_name}"}

    # A profile is untrusted XML (users copy these files around): reject
    # entity-expansion / external entities before the content is written over
    # the live ModsConfig.xml.
    try:
        parse_xml_root(src)
    except Exception as e:
        return {"ok": False, "msg": f"Profile 不是有效的 XML: {e}"}

    backup = target_path.with_suffix(".xml.rwmod.bak")
    tmp: Path | None = None
    try:
        content = src.read_text(encoding="utf-8")
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write a temp file FIRST, then atomically rename it over
        # the live file. The live ModsConfig.xml is *copied* to .bak, never
        # moved: an earlier version moved it aside first, so a failure at the
        # second step left RimWorld with no ModsConfig.xml at all. The temp
        # name is unique per writer (REV-010): a fixed name let two concurrent
        # restores share one scratch file. A scratch file left by a crashed
        # pre-fix writer is removed so it can never be mistaken for data.
        _remove_legacy_temp(target_path)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{target_path.name}.",
            suffix=".rwmod.tmp",
            dir=target_path.parent,
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)

        if target_path.exists():
            # A directory squatting on the .bak name would make copy2 place
            # the file *inside* it and report success — refuse instead.
            if backup.exists() and not backup.is_file():
                raise OSError(f"备份路径被占用: {backup}")
            shutil.copy2(target_path, backup)
            _log.info("备份现有 ModsConfig.xml → %s", backup.name)

        os.replace(tmp, target_path)
        mod_count = _count_mods_in_xml(src)
        _log.info("恢复 profile: %s → %s (%d mods)", safe_name, target_path, mod_count)
        return {
            "ok": True,
            "msg": f"已恢复 '{safe_name}'（{mod_count} 个 Mod）",
            "mod_count": mod_count,
        }
    except Exception as e:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        # os.replace is atomic, so a failure normally leaves the live file
        # intact. If it disappeared anyway (external interference), restore the
        # .bak so the game never loses its mod list.
        if not target_path.exists() and backup.exists():
            os.replace(backup, target_path)
        return {"ok": False, "msg": f"恢复失败: {e}"}


def delete_profile(name: str, instance_id: str | None = None) -> bool:
    """Delete a saved profile."""
    safe_name = safe_filename(name)
    path = profiles_dir_for(instance_id, create=False) / f"{safe_name}.xml"
    _assert_within_sandbox(path)
    if not path.exists():
        return False
    path.unlink()
    _log.info("删除 profile: %s", safe_name)
    return True


# ── internal helpers ───────────────────────────────────────────────


def _count_mods_in_xml(path: Path) -> int:
    """Count <li> elements under <activeMods> in a ModsConfig.xml."""
    try:
        # defused parse: a profile file is user-supplied content, so entity
        # bombs / XXE must be rejected by the safe parser, not stdlib.
        root = parse_xml_root(path)
        active = root.find("activeMods")
        if active is not None:
            return len(active.findall("li"))
    except Exception:
        pass
    return 0
