"""Configuration management - reads/writes ~/.rwmod.toml.

Config format v2 adds a ``[[instances]]`` array plus ``active_instance``:

.. code-block:: toml

    active_instance = "default"
    steamcmd_path = "..."   # flat mirror of the active instance (v1 compat)
    mods_dir = "..."

    [[instances]]
    id = "default"
    name = "默认实例"
    steamcmd_path = "..."
    mods_dir = "..."
    rimworld_dir = "..."
    backup_dir = "..."

Old (v1) flat files keep working: the flat path keys are synthesised into a
single ``default`` instance on load, and :meth:`Config.save` always writes both
the instance tables *and* the flat mirror of the active instance, so an older
rwmod build keeps operating on the same directories.

The flat attributes (``cfg.mods_dir``, ``cfg.rimworld_dir`` …) remain the
public API and are properties that read/write the *active* instance. That is
what lets every existing call site keep working unchanged.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rwmod.errors import ConfigError
from rwmod.steam_db import MAX_CACHE_BYTES as STEAM_DB_MAX_BYTES
from rwmod.utils import bundle_root

__all__ = [
    "Config",
    "InstanceConfig",
    "current_instance_id",
    "is_valid_instance_id",
    "resolve_instance_id",
]

DEFAULT_INSTANCE_ID = "default"

#: Instance ids double as directory names under ~/.rwmod/{profiles,undo}/, so
#: they must never contain a path separator or resolve to "." / "..".
_INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def is_valid_instance_id(value: str) -> bool:
    """True if *value* is a safe instance id (also usable as a directory name)."""
    return bool(_INSTANCE_ID_RE.fullmatch(value)) and value not in (".", "..")


@dataclass
class InstanceConfig:
    """One RimWorld installation / mods directory pair."""

    id: str
    name: str = ""
    steamcmd_path: Path = field(default_factory=lambda: Path("D:/steamcmd/steamcmd.exe"))
    mods_dir: Path = field(default_factory=lambda: Path("D:/RimWorld/Mods"))
    rimworld_dir: Path = field(default_factory=lambda: Path("D:/RimWorld"))
    backup_dir: Path | None = None
    config_dir: Path | None = None

    def __post_init__(self) -> None:
        self.steamcmd_path = Path(self.steamcmd_path)
        self.mods_dir = Path(self.mods_dir)
        self.rimworld_dir = Path(self.rimworld_dir)
        self.backup_dir = Path(self.backup_dir) if self.backup_dir else self.mods_dir / "_backups"
        self.config_dir = Path(self.config_dir) if self.config_dir else None


class Config:
    """Persisted in ~/.rwmod.toml, defaults to D: drive paths."""

    CONFIG_PATH = Path.home() / ".rwmod.toml"

    @classmethod
    def _default_steamcmd_path(cls) -> Path:
        """Lazily resolve the built-in steamcmd path (source / frozen aware).

        Frozen (PyInstaller) layouts:
          1. Installed next to rwmod.exe ({app}\\steamcmd\\steamcmd.exe) — the
             layout produced by rwmod.iss.
          2. Bundled copy inside the one-file extraction dir (sys._MEIPASS).
        """
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            p = exe_dir / "steamcmd" / "steamcmd.exe"
            if p.exists():
                return p
            if hasattr(sys, "_MEIPASS"):
                p = Path(sys._MEIPASS) / "steamcmd" / "steamcmd.exe"
                if p.exists():
                    return p
            return exe_dir / "steamcmd" / "steamcmd.exe"

        p = bundle_root() / "steamcmd" / "steamcmd.exe"
        return p if p.exists() else Path("D:/steamcmd/steamcmd.exe")

    def __init__(
        self,
        steamcmd_path: Path | None = None,
        mods_dir: Path = Path("D:/RimWorld/Mods"),
        rimworld_dir: Path = Path("D:/RimWorld"),
        backup_dir: Path | None = None,
        steam_api_key: str = "",
        update_backup: bool = True,
        backup_keep: int = 3,
        enable_skymods: bool = False,
        config_dir: Path | None = None,
        enable_steam_db: bool = False,
        steam_db_max_bytes: int = STEAM_DB_MAX_BYTES,
        *,
        instances: list[InstanceConfig] | None = None,
        active_instance: str | None = None,
    ) -> None:
        if steamcmd_path is None:
            steamcmd_path = Config._default_steamcmd_path()
        if instances:
            self._instances = self._coerce_instances(instances)
        else:
            self._instances = [
                InstanceConfig(
                    id=DEFAULT_INSTANCE_ID,
                    name="默认实例",
                    steamcmd_path=steamcmd_path,
                    mods_dir=mods_dir,
                    rimworld_dir=rimworld_dir,
                    backup_dir=backup_dir or (mods_dir / "_backups"),
                    config_dir=config_dir,
                )
            ]
        ids = {inst.id for inst in self._instances}
        requested = active_instance or DEFAULT_INSTANCE_ID
        self._active_id = requested if requested in ids else self._instances[0].id

        self.steam_api_key = steam_api_key
        # P1-2: take a persistent zip backup before every force overwrite and
        # keep at most ``backup_keep`` per mod. Old config files have neither
        # key, so both must default to the behaviour we want for them.
        self.update_backup = update_backup
        self.backup_keep = max(1, int(backup_keep))
        # P2-2: the Skymods fallback is opt-in — smods.ru sits behind a
        # Cloudflare challenge that plain urllib cannot pass (observed 403).
        self.enable_skymods = enable_skymods
        # Wave 5a: the 47 MB Steam Workshop database is opt-in. ``False`` keeps
        # every existing response byte-identical (no cache is consulted);
        # downloading is always an explicit POST /api/rimsort/steamdb/refresh.
        self.enable_steam_db = enable_steam_db
        self.steam_db_max_bytes = max(1, int(steam_db_max_bytes))

    # ── instance accessors ─────────────────────────────────────────

    @classmethod
    def _coerce_instances(cls, raw: list[InstanceConfig]) -> list[InstanceConfig]:
        """Copy *raw*, dropping unsafe/duplicate ids (a hand-edited TOML)."""
        out: list[InstanceConfig] = []
        seen: set[str] = set()
        for inst in raw:
            if not is_valid_instance_id(inst.id) or inst.id in seen:
                continue
            seen.add(inst.id)
            out.append(inst)
        return out

    @property
    def instances(self) -> list[InstanceConfig]:
        """All configured instances, in file order (the first is the fallback)."""
        return list(self._instances)

    @property
    def active_instance(self) -> str:
        """Id of the instance the flat properties point at."""
        return self._active_id

    @property
    def active(self) -> InstanceConfig:
        """The active :class:`InstanceConfig`."""
        for inst in self._instances:
            if inst.id == self._active_id:
                return inst
        # Defensive: _active_id is always kept in sync by the methods below.
        return self._instances[0]

    def get_instance(self, instance_id: str) -> InstanceConfig | None:
        for inst in self._instances:
            if inst.id == instance_id:
                return inst
        return None

    def activate_instance(self, instance_id: str) -> None:
        """Make *instance_id* active. Raises KeyError for an unknown id."""
        if self.get_instance(instance_id) is None:
            raise KeyError(instance_id)
        self._active_id = instance_id

    def add_instance(
        self,
        *,
        instance_id: str | None = None,
        name: str | None = None,
        steamcmd_path: Path | None = None,
        mods_dir: Path = Path("D:/RimWorld/Mods"),
        rimworld_dir: Path = Path("D:/RimWorld"),
        backup_dir: Path | None = None,
        config_dir: Path | None = None,
    ) -> InstanceConfig:
        """Append a new instance. Raises ValueError for an invalid/duplicate id."""
        if instance_id is None:
            instance_id = self._generate_instance_id(name)
        if not is_valid_instance_id(instance_id):
            raise ValueError(f"非法实例 id: {instance_id!r}")
        if self.get_instance(instance_id) is not None:
            raise ValueError(f"实例已存在: {instance_id}")
        inst = InstanceConfig(
            id=instance_id,
            name=(name or "").strip() or instance_id,
            steamcmd_path=steamcmd_path or self._default_steamcmd_path(),
            mods_dir=Path(mods_dir),
            rimworld_dir=Path(rimworld_dir),
            backup_dir=Path(backup_dir) if backup_dir else Path(mods_dir) / "_backups",
            config_dir=Path(config_dir) if config_dir else None,
        )
        self._instances.append(inst)
        return inst

    def remove_instance(self, instance_id: str) -> None:
        """Delete a non-active instance. Raises KeyError/ValueError."""
        inst = self.get_instance(instance_id)
        if inst is None:
            raise KeyError(instance_id)
        if instance_id == self._active_id:
            raise ValueError("不能删除活动实例")
        if len(self._instances) <= 1:
            raise ValueError("至少保留一个实例")
        self._instances.remove(inst)

    def _generate_instance_id(self, name: str | None) -> str:
        base = re.sub(r"[^a-z0-9_-]+", "-", (name or "instance").strip().lower())
        base = base.strip("-_")[:48]
        if not base or not base[0].isalnum():
            base = "instance"
        candidate = base
        suffix = 2
        while self.get_instance(candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    # ── flat properties (active instance) ──────────────────────────

    @property
    def steamcmd_path(self) -> Path:
        return self.active.steamcmd_path

    @steamcmd_path.setter
    def steamcmd_path(self, value: Path) -> None:
        self.active.steamcmd_path = Path(value)

    @property
    def mods_dir(self) -> Path:
        return self.active.mods_dir

    @mods_dir.setter
    def mods_dir(self, value: Path) -> None:
        self.active.mods_dir = Path(value)

    @property
    def rimworld_dir(self) -> Path:
        return self.active.rimworld_dir

    @rimworld_dir.setter
    def rimworld_dir(self, value: Path) -> None:
        self.active.rimworld_dir = Path(value)

    @property
    def backup_dir(self) -> Path:
        inst = self.active
        # InstanceConfig.__post_init__ always fills this in; the fallback only
        # guards against a caller assigning None through an old code path.
        return inst.backup_dir or (inst.mods_dir / "_backups")

    @backup_dir.setter
    def backup_dir(self, value: Path | None) -> None:
        self.active.backup_dir = Path(value) if value else self.active.mods_dir / "_backups"

    @property
    def config_dir(self) -> Path | None:
        """Explicit directory holding ModsConfig.xml (None = autodetect)."""
        return self.active.config_dir

    @config_dir.setter
    def config_dir(self, value: Path | None) -> None:
        self.active.config_dir = Path(value) if value is not None else None

    # ── persistence ────────────────────────────────────────────────

    @classmethod
    def load(cls) -> Config:
        # ── in-process cache: avoid re-reading ~/.rwmod.toml on every request ──
        # Config.load() is called on every API request via FastAPI dependency.
        # The file rarely changes mid-process (only via the settings API which
        # calls save()), so a TTL cache eliminates disk I/O per request.
        #
        # CONFIG_PATH is part of the cache key so tests that patch CONFIG_PATH
        # (e.g. to a tmp_path) always load from the *patched* path, never a
        # stale cached instance created under a different CONFIG_PATH.
        now = __import__("time").monotonic()
        if (
            cls._cache is not None
            and cls._cache_path_key == cls.CONFIG_PATH
            and (now - cls._cache_ts) < cls._CACHE_TTL
        ):
            return cls._cache

        cfg = cls._load_no_cache()
        cls._cache = cfg
        cls._cache_ts = now
        cls._cache_path_key = cls.CONFIG_PATH
        return cfg

    @classmethod
    def _load_no_cache(cls) -> Config:
        builtin = cls._default_steamcmd_path()
        if cls.CONFIG_PATH.exists():
            data = tomllib.loads(cls.CONFIG_PATH.read_text(encoding="utf-8"))
            instances = cls._instances_from_toml(data, builtin)
            if instances is not None:
                return cls(
                    steamcmd_path=builtin,
                    steam_api_key=str(data.get("steam_api_key", "")),
                    update_backup=bool(data.get("update_backup", True)),
                    backup_keep=int(data.get("backup_keep", 3)),
                    enable_skymods=bool(data.get("enable_skymods", False)),
                    enable_steam_db=bool(data.get("enable_steam_db", False)),
                    steam_db_max_bytes=int(data.get("steam_db_max_bytes", STEAM_DB_MAX_BYTES)),
                    instances=instances,
                    active_instance=str(data.get("active_instance", "")),
                )
            # v1 file: synthesise the single default instance from flat keys.
            sc = Path(str(data.get("steamcmd_path", str(builtin))))
            # Honor the user's configured steamcmd_path when it actually
            # exists. The bundled copy is only a fallback for a stale/broken
            # configured path (e.g. an old install pointing at a deleted
            # D:/steamcmd) — silently overriding a valid user setting would
            # make the config panel's steamcmd field appear to do nothing.
            if builtin.exists() and not sc.exists():
                sc = builtin
            return cls(
                steamcmd_path=sc,
                mods_dir=Path(data.get("mods_dir", "D:/RimWorld/Mods")),
                rimworld_dir=Path(data.get("rimworld_dir", "D:/RimWorld")),
                backup_dir=Path(data["backup_dir"]) if "backup_dir" in data else None,
                steam_api_key=data.get("steam_api_key", ""),
                update_backup=bool(data.get("update_backup", True)),
                backup_keep=int(data.get("backup_keep", 3)),
                enable_skymods=bool(data.get("enable_skymods", False)),
                config_dir=Path(data["config_dir"]) if data.get("config_dir") else None,
                enable_steam_db=bool(data.get("enable_steam_db", False)),
                steam_db_max_bytes=int(data.get("steam_db_max_bytes", STEAM_DB_MAX_BYTES)),
            )
        return cls(steamcmd_path=builtin)

    @classmethod
    def _instances_from_toml(cls, data: dict, builtin: Path) -> list[InstanceConfig] | None:
        """Parse ``[[instances]]``; None means "v1 flat file, synthesise one"."""
        raw = data.get("instances")
        if not isinstance(raw, list) or not raw:
            return None
        out: list[InstanceConfig] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id") or "").strip()
            if not is_valid_instance_id(iid) or iid in seen:
                continue
            sc_raw = item.get("steamcmd_path")
            sc = Path(str(sc_raw)) if sc_raw else builtin
            if builtin.exists() and not sc.exists():
                sc = builtin
            mods_raw = item.get("mods_dir")
            rw_raw = item.get("rimworld_dir")
            out.append(
                InstanceConfig(
                    id=iid,
                    name=str(item.get("name") or iid),
                    steamcmd_path=sc,
                    mods_dir=Path(str(mods_raw)) if mods_raw else Path("D:/RimWorld/Mods"),
                    rimworld_dir=Path(str(rw_raw)) if rw_raw else Path("D:/RimWorld"),
                    backup_dir=Path(str(item["backup_dir"])) if item.get("backup_dir") else None,
                    config_dir=Path(str(item["config_dir"])) if item.get("config_dir") else None,
                )
            )
            seen.add(iid)
        return out or None

    def save(self) -> None:
        self.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # TOML requires every top-level key to precede the first table header,
        # so the v1-compatible flat mirror goes first and [[instances]] last.
        lines = [f'active_instance = "{_esc(self.active_instance)}"']
        lines.extend(
            [
                f'steamcmd_path = "{_esc(self.steamcmd_path.as_posix())}"',
                f'mods_dir = "{_esc(self.mods_dir.as_posix())}"',
                f'rimworld_dir = "{_esc(self.rimworld_dir.as_posix())}"',
                f'backup_dir = "{_esc(self.backup_dir.as_posix())}"',
            ]
        )
        if self.config_dir is not None:
            lines.append(f'config_dir = "{_esc(self.config_dir.as_posix())}"')
        if self.steam_api_key:
            lines.append(f'steam_api_key = "{_esc(self.steam_api_key)}"')
        # Only write non-default values — old config files stay minimal and
        # readable by older builds (which ignore unknown keys).
        if not self.update_backup:
            lines.append("update_backup = false")
        if self.backup_keep != 3:
            lines.append(f"backup_keep = {self.backup_keep}")
        if self.enable_skymods:
            lines.append("enable_skymods = true")
        if self.enable_steam_db:
            lines.append("enable_steam_db = true")
        if self.steam_db_max_bytes != STEAM_DB_MAX_BYTES:
            lines.append(f"steam_db_max_bytes = {self.steam_db_max_bytes}")

        for inst in self._instances:
            lines.append("")
            lines.append("[[instances]]")
            lines.append(f'id = "{_esc(inst.id)}"')
            lines.append(f'name = "{_esc(inst.name)}"')
            lines.append(f'steamcmd_path = "{_esc(inst.steamcmd_path.as_posix())}"')
            lines.append(f'mods_dir = "{_esc(inst.mods_dir.as_posix())}"')
            lines.append(f'rimworld_dir = "{_esc(inst.rimworld_dir.as_posix())}"')
            if inst.backup_dir:
                lines.append(f'backup_dir = "{_esc(inst.backup_dir.as_posix())}"')
            if inst.config_dir is not None:
                lines.append(f'config_dir = "{_esc(inst.config_dir.as_posix())}"')

        self.CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # ── invalidate the in-process cache so the next load() sees new values ──
        cls = type(self)
        cls._cache = None
        cls._cache_ts = 0.0
        cls._cache_path_key = None

    def validate(self) -> None:
        """Validate minimal requirements for core operations.

        SteamCMD is required for downloads; the mods dir is created lazily.
        RimWorld game dir is *not* required here — it is only needed by
        optional features (compat / load-order / saves) which handle its
        absence themselves.
        """
        if not self.steamcmd_path.exists():
            raise ConfigError(f"SteamCMD not found: {self.steamcmd_path}")
        self.mods_dir.mkdir(parents=True, exist_ok=True)

    # ── in-process cache state ──────────────────────────────────────
    _cache: Config | None = None
    _cache_ts: float = 0.0
    _cache_path_key: Path | None = None
    # Seconds — long enough to dedupe request bursts, short enough that a
    # manual edit to ~/.rwmod.toml is picked up quickly.
    _CACHE_TTL: float = 2.0

    # ── instance bookkeeping ────────────────────────────────────────
    _instances: list[InstanceConfig]
    _active_id: str


def _esc(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def current_instance_id() -> str:
    """Active instance id, or ``"default"`` when no config can be loaded.

    Used by the scoped stores (database/tags/profile/undo) so they follow the
    instance the user switched to without every call site having to pass an id.
    """
    try:
        return Config.load().active_instance
    except Exception:  # pragma: no cover — fail open to the default scope
        return DEFAULT_INSTANCE_ID


def resolve_instance_id(mods_dir: Path | str | None = None) -> str:
    """Return the instance that owns *mods_dir*, else the active instance.

    Exact path matching lets a caller that still only passes ``cfg.mods_dir``
    (there are ~86 of them) hit the right per-instance cache/tag scope.
    """
    try:
        cfg = Config.load()
    except Exception:  # pragma: no cover — fail open to the default scope
        return DEFAULT_INSTANCE_ID
    if mods_dir is not None:
        target = _norm_dir(mods_dir)
        for inst in cfg.instances:
            if _norm_dir(inst.mods_dir) == target:
                return inst.id
    return cfg.active_instance


def _norm_dir(value: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(value)))
