"""Shared test fixtures — user-path isolation, TestClient, temp config, temp mods dir."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rwmod.config import Config
from rwmod.database import close_db

#: Env marker set for the duration of an *outer*, explicitly-established
#: sandbox redirect (currently only ``tests/test_probe_e2e.py::_isolated_home``,
#: which owns the redirect for a whole class and must not be overridden per
#: test). The value is the sandbox home directory.
_SANDBOX_ENV_VAR = "RWMOD_TEST_SANDBOX_HOME"


def _outer_sandbox_home() -> Path | None:
    """Return the root of an already-active outer sandbox, else ``None``.

    The env var alone is not trusted: every path ``_isolate_user_paths`` would
    otherwise redirect must *already* point inside the declared root — if it
    does not, the marker is stale (or the paths were restored) and the check
    fails closed by returning ``None``, so normal per-test isolation proceeds.
    """
    raw = os.environ.get(_SANDBOX_ENV_VAR)
    if not raw:
        return None
    import rwmod.config as cfg_mod
    import rwmod.database as db_mod
    import rwmod.logger as log_mod
    import rwmod.profile as profile_mod
    import rwmod.transfer as transfer_mod
    import rwmod.undo as undo_mod

    root = Path(raw)
    live = (
        Path.home(),
        cfg_mod.Config.CONFIG_PATH,
        db_mod.DB_PATH,
        log_mod.LOG_PATH,
        profile_mod.PROFILES_DIR,
        undo_mod.UNDO_DIR,
        transfer_mod.PROFILES_DIR,
    )
    try:
        if all(Path(p).is_relative_to(root) for p in live):
            return root
    except (TypeError, ValueError):  # pragma: no cover — non-path would be a bug
        pass
    return None


@pytest.fixture(autouse=True)
def _isolate_user_paths(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Redirect every home-bound path into a per-test scratch dir for *every* test.

    Only tests that requested the ``client`` fixture used to be isolated;
    everything else read and wrote the developer's real ``~/.rwmod.db``,
    ``~/.rwmod.log`` and ``~/.rwmod/`` (a real ``~/.rwmod.db`` accumulated test
    rows this way, and a stale per-thread connection then made unrelated queue
    tests see the real queue). This fixture is autouse so no test can opt out,
    with the single explicitly-verified exception described below.

    Two categories of paths need redirecting:

    * ``Path.home()`` is consulted at *call* time by ``resolve_modsconfig_path``,
      ``save_parser`` and ``RimPyDB()`` — patching ``Path.home`` covers those.
    * ``Config.CONFIG_PATH`` / ``database.DB_PATH`` / ``logger.LOG_PATH`` /
      ``profile.PROFILES_DIR`` / ``undo.UNDO_DIR`` (and ``transfer``'s own
      imported copy) are computed from ``Path.home()`` at *import* time, so the
      ``Path.home`` patch alone cannot reach them — each module attribute is
      patched explicitly.

    The one deliberate exception is an *outer* sandbox that has already
    redirected every one of those paths itself — currently the class-scoped
    E2E fixture in ``tests/test_probe_e2e.py``. It announces itself through
    ``RWMOD_TEST_SANDBOX_HOME``, and this fixture then stands down for that
    test instead of clobbering the redirect the app under test was started
    with. ``_outer_sandbox_home`` verifies the marker is genuinely live, so a
    stale marker cannot silently disable isolation.
    """
    import rwmod.config as cfg_mod
    import rwmod.database as db_mod
    import rwmod.deps as deps_mod
    import rwmod.logger as log_mod
    import rwmod.profile as profile_mod
    import rwmod.queue as q_mod
    import rwmod.rimpy_db as rimpy_mod
    import rwmod.transfer as transfer_mod
    import rwmod.undo as undo_mod

    # An outer fixture may already own the redirect for this test — and, being
    # class-scoped, for its whole class (tests/test_probe_e2e.py::e2e). It
    # announces itself through _SANDBOX_ENV_VAR; standing down is the only safe
    # option there, because this fixture's per-test home has an *empty* DB while
    # the app under test was started against the outer sandbox's initialised
    # one — re-pointing DB_PATH at the empty file is what made every E2E query
    # fail with "no such table: download_history". _outer_sandbox_home() only
    # reports a sandbox whose paths are all verifiably live, so a stale marker
    # cannot silently disable isolation.
    if _outer_sandbox_home() is not None:
        yield
        return

    # Deliberately outside tmp_path: several tests treat tmp_path itself as an
    # empty scratch dir (e.g. skymods._find_mod_folder returns the first
    # subdirectory when no About.xml is found), and probe fixtures already use
    # the name tmp_path/"home" for their own sandbox.
    home = tmp_path_factory.mktemp("_rwmod_home")

    # Everything that resolves ~ at call time.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # Constants bound from Path.home() at import time.
    monkeypatch.setattr(cfg_mod.Config, "CONFIG_PATH", home / ".rwmod.toml")
    monkeypatch.setattr(db_mod, "DB_PATH", home / ".rwmod.db")
    monkeypatch.setattr(log_mod, "LOG_PATH", home / ".rwmod.log")
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", home / ".rwmod" / "profiles")
    monkeypatch.setattr(undo_mod, "UNDO_DIR", home / ".rwmod" / "undo")
    # transfer.py does `from rwmod.profile import PROFILES_DIR`, so it holds a
    # second copy of the constant that patching profile alone does not reach.
    monkeypatch.setattr(transfer_mod, "PROFILES_DIR", home / ".rwmod" / "profiles")

    # A connection opened against a previous test's DB_PATH (or the real one)
    # stays cached in rwmod.database's per-thread pool; get_conn() would return
    # it without ever consulting the patched DB_PATH. Drop the pool first.
    close_db()

    # Singletons that captured a config/path in an earlier test. Set to the
    # clean "not created yet" state; restoring a stale instance afterwards
    # would be worse than leaving these unset.
    cfg_mod.Config._cache = None
    cfg_mod.Config._cache_ts = 0.0
    cfg_mod.Config._cache_path_key = None
    q_mod._queue = None
    deps_mod._autoupdate = None
    rimpy_mod.RimPyDB._instance = None

    # steamcmd_support keeps a process-global set of *pending* cancel requests
    # (an id recorded before its batch reaches Popen). A test that cancels a
    # fabricated queue item never dispatches a batch, so its marker would
    # cancel the next test's download of the same workshop id. Reset it around
    # every test, exactly like the live-process registries below.
    import rwmod.steamcmd_support.procs as procs_mod

    procs_mod._cancel_requests.clear()

    # rwmod.server calls init_logging() at import, attaching a RotatingFileHandler
    # for the real ~/.rwmod.log to the *root* logger. That handler survives later
    # LOG_PATH patches and would keep writing the developer's log from every
    # subsequent test, so drop leftover file handlers. Tests that assert on
    # logging (test_logger.py) call init_logging() themselves with a patched path.
    _detach_root_file_handlers()

    yield

    procs_mod._cancel_requests.clear()
    close_db()


def _detach_root_file_handlers() -> None:
    """Remove (and close) every FileHandler currently attached to the root logger."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture(autouse=True)
def _reset_global_caches() -> Iterator[None]:
    """Drop process-global mod caches around every test.

    ``mod_cache`` keeps a TTL memory layer and ``mod_sizes`` keeps per-Mods-dir
    snapshots plus daemon scan threads; both outlive a test. Without this a
    later test can observe a previous test's mod list or size snapshot, or race
    a scan that is still walking a directory the test has just removed.
    Deliberately separate from ``_isolate_user_paths``: that fixture stands
    down for the class-scoped E2E outer sandbox, while this cleanup is wanted
    for every test, including E2E.
    """
    import rwmod.mod_cache as mod_cache_mod
    import rwmod.mod_sizes as mod_sizes_mod

    mod_sizes_mod.stop_all()
    mod_sizes_mod.invalidate()
    mod_cache_mod.clear_memory_cache()
    yield
    mod_sizes_mod.stop_all()
    mod_sizes_mod.invalidate()
    mod_cache_mod.clear_memory_cache()


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

    # profile.resolve_modsconfig_path() consults the user's LocalLow profile
    # *before* it ever looks at cfg.rimworld_dir, so without this the suite reads
    # the developer's real RimWorld ModsConfig.xml — results then depend on the
    # machine and on whatever the developer happens to have installed.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    # monkeypatch (not manual save/restore) so a failing test cannot skip the
    # restore and leak a dead tmp path into later tests.
    monkeypatch.setattr(cfg_mod.Config, "CONFIG_PATH", tmp_path / ".rwmod_test.toml")
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / ".rwmod_test.db")

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
    # Config() derived backup_dir from the *default* mods_dir (D:/RimWorld/Mods)
    # before mods_dir was reassigned above, so without this every test that
    # exercises backup_mod()/cleanup/restore writes into the developer's real
    # D:/RimWorld/Mods/_backups — the exact data the test brief forbids touching.
    cfg.backup_dir = tmp_path / "Backups"
    cfg.mods_dir.mkdir()
    cfg.rimworld_dir.mkdir()
    cfg.save()

    from rwmod.server import app

    with TestClient(app) as tc:
        yield tc

    # Path constants are restored by monkeypatch; just drop the connections.
    close_db()
