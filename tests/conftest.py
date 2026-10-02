"""Shared test fixtures — TestClient, temp config, temp mods dir."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rwmod.config import Config
from rwmod.database import close_db


@pytest.fixture(autouse=True)
def _reset_offline_state() -> Iterator[None]:
    """Connectivity state is process-global and is written by real request
    outcomes, so a test that makes a fetch fail would otherwise leave the next
    test believing Steam is unreachable."""
    import rwmod.offline as offline_mod

    offline_mod._is_online = True
    offline_mod._last_check_time = 0
    yield
    offline_mod._is_online = True
    offline_mod._last_check_time = 0


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a TestClient with an isolated config and temp DB."""
    # Override Config.CONFIG_PATH and DB_PATH
    import rwmod.config as cfg_mod
    import rwmod.database as db_mod

    orig_config = cfg_mod.Config.CONFIG_PATH
    orig_db = db_mod.DB_PATH

    # profile.resolve_modsconfig_path() consults the user's LocalLow profile
    # *before* it ever looks at cfg.rimworld_dir, so without this the suite reads
    # the developer's real RimWorld ModsConfig.xml — results then depend on the
    # machine and on whatever the developer happens to have installed.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    cfg_mod.Config.CONFIG_PATH = tmp_path / ".rwmod_test.toml"
    db_mod.DB_PATH = tmp_path / ".rwmod_test.db"

    # Reset singleton queue
    import rwmod.queue as q_mod

    q_mod._queue = None

    # Reset autoupdate singleton
    import rwmod.deps as deps_mod

    deps_mod._autoupdate = None

    # Write a minimal valid config
    cfg = Config(steamcmd_path=tmp_path / "steamcmd" / "steamcmd.exe")
    (tmp_path / "steamcmd").mkdir()
    (tmp_path / "steamcmd" / "steamcmd.exe").touch()
    cfg.mods_dir = tmp_path / "Mods"
    cfg.rimworld_dir = tmp_path / "RimWorld"
    cfg.mods_dir.mkdir()
    cfg.rimworld_dir.mkdir()
    cfg.save()

    from rwmod.server import app

    with TestClient(app) as tc:
        yield tc

    # Cleanup
    cfg_mod.Config.CONFIG_PATH = orig_config
    db_mod.DB_PATH = orig_db
    close_db()
