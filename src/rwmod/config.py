"""Configuration management - reads/writes ~/.rwmod.toml."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from rwmod.errors import ConfigError
from rwmod.utils import bundle_root

__all__ = ["Config"]


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
    ) -> None:
        if steamcmd_path is None:
            steamcmd_path = Config._default_steamcmd_path()
        self.steamcmd_path = steamcmd_path
        self.mods_dir = mods_dir
        self.rimworld_dir = rimworld_dir
        self.backup_dir = backup_dir or (mods_dir / "_backups")
        self.steam_api_key = steam_api_key

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
            sc = Path(data.get("steamcmd_path", str(builtin)))
            if builtin.exists():
                sc = builtin
            return cls(
                steamcmd_path=sc,
                mods_dir=Path(data.get("mods_dir", "D:/RimWorld/Mods")),
                rimworld_dir=Path(data.get("rimworld_dir", "D:/RimWorld")),
                backup_dir=Path(data["backup_dir"]) if "backup_dir" in data else None,
                steam_api_key=data.get("steam_api_key", ""),
            )
        return cls(steamcmd_path=builtin)

    def save(self) -> None:
        self.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f'steamcmd_path = "{_esc(self.steamcmd_path.as_posix())}"',
            f'mods_dir = "{_esc(self.mods_dir.as_posix())}"',
            f'rimworld_dir = "{_esc(self.rimworld_dir.as_posix())}"',
        ]
        if self.backup_dir:
            lines.append(f'backup_dir = "{_esc(self.backup_dir.as_posix())}"')
        if self.steam_api_key:
            lines.append(f'steam_api_key = "{_esc(self.steam_api_key)}"')
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


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
