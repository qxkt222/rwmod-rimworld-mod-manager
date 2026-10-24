"""E2E-* probes: real SteamCMD download chain against a fully isolated install.

This is the *end-to-end* probe: it drives the real FastAPI app through
TestClient while the real bundled ``steamcmd/steamcmd.exe`` performs real
anonymous Workshop downloads. No mocking of SteamCMD, HTTP, or the filesystem.

── Why the default gate skips it ──────────────────────────────────────
Every test carries ``@pytest.mark.network`` (``run_tests.py`` runs pytest with
``-m "not network"``), and an extra opt-in interlock requires
``RWMOD_E2E_ALLOW_REAL=1``. Two independent switches, because this file spends
real network bandwidth and launches real SteamCMD processes.

── Prerequisites ──────────────────────────────────────────────────────
1. The frontend must be built (``python tools/build_frontend.py``) — one test
   asserts ``GET /`` returns 200 rather than the 503 "frontend not built" page.
2. ``steamcmd/steamcmd.exe`` must exist and be logged in for anonymous access
   at least once (network + Steam reachable).
3. Workshop id ``3679611484`` must be present in
   ``steamcmd/steamapps/workshop/content/294100/``. It is the smallest cached
   item that is a *valid* mod (has ``About/About.xml``) and declares **no
   dependencies**, so a run exercises the SteamCMD cache-hit fast path and
   downloads nothing else. Override with ``RWMOD_E2E_MOD_ID`` to use another
   cached id (pick a zero-dependency one, or the test will recursively fetch
   its dependency graph).

── How to run manually ────────────────────────────────────────────────
From the repo root, in Git Bash::

    RWMOD_E2E_ALLOW_REAL=1 ./.venv/Scripts/python.exe -m pytest \\
        tests/test_probe_e2e.py -p no:cacheprovider --no-cov -q

To guarantee the run cannot fall back on the developer's real ``~/.rwmod*``
even if a fixture is bypassed, point the process home at a scratch dir::

    mkdir -p D:/AgentSandbox/rwmod-e2e/home
    USERPROFILE='D:\\AgentSandbox\\rwmod-e2e\\home' \\
    PYTHONIOENCODING=utf-8 RWMOD_E2E_ALLOW_REAL=1 \\
    ./.venv/Scripts/python.exe -m pytest tests/test_probe_e2e.py \\
        -p no:cacheprovider --no-cov -q

── Isolation ──────────────────────────────────────────────────────────
Mirrors ``tests/conftest.py``: ``Path.home`` is redirected and every
module-level home-bound constant (CONFIG_PATH, DB_PATH, LOG_PATH,
PROFILES_DIR, UNDO_DIR) is redirected with it, plus the queue/autoupdate
singletons are reset. ``mods_dir`` / ``backup_dir`` live under ``tmp_path``.
The real ``D:/RimWorld/Mods``, ``~/.rwmod.toml``, ``~/.rwmod.db``,
``~/.rwmod.log`` and ``~/.rwmod/`` are never written.

The one shared resource this file *does* write is the repo's own SteamCMD
install (``steamcmd/``): SteamCMD rewrites
``steamapps/workshop/appworkshop_294100.acf`` and appends to
``logs/workshop_log.txt`` on every run. That directory is gitignored and is
the machine-local cache the app itself uses — there is no way to exercise the
cache-hit path without it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from rwmod.utils import bundle_root

pytestmark = pytest.mark.network

# Smallest valid, dependency-free cached mod (8494 bytes on Steam).
DEFAULT_MOD_ID = "3679611484"
# Its About.xml packageId, which becomes the installed folder name.
DEFAULT_PKG_ID = "ZH.Arkymn.PerformanceEsmolas"

_ROUND_TRIP_TIMEOUT_S = 600.0

#: Announcement read by ``tests/conftest.py``. The class-scoped ``e2e`` fixture
#: below redirects every home-bound path for a whole class; conftest's autouse
#: ``_isolate_user_paths`` fixture would otherwise re-redirect them per test,
#: pointing the app at a fresh empty DB while the tables were created in this
#: sandbox's DB (``no such table: download_history``). While this marker is set
#: — and the live constants verifiably point inside it — conftest stands down.
SANDBOX_ENV_VAR = "RWMOD_TEST_SANDBOX_HOME"


def _require_opt_in(mod_id: str) -> None:
    if os.environ.get("RWMOD_E2E_ALLOW_REAL") != "1":
        pytest.skip("set RWMOD_E2E_ALLOW_REAL=1 to run real-network E2E probes")
    if not (bundle_root() / "steamcmd" / "steamcmd.exe").exists():
        pytest.skip("bundled steamcmd.exe not present")
    if not (
        bundle_root() / "steamcmd" / "steamapps" / "workshop" / "content" / "294100" / mod_id
    ).exists():
        pytest.skip(f"workshop id {mod_id} is not in the local SteamCMD cache")


@contextlib.contextmanager
def _isolated_home(home: Path) -> Iterator[None]:
    """Redirect every home-bound path constant into *home* for the block.

    Restores module state afterwards so the rest of the suite is unaffected —
    the same list conftest.py patches, plus the constants it does not
    (LOG_PATH / PROFILES_DIR / UNDO_DIR are bound at import time, so patching
    ``Path.home`` alone is not enough for them).

    While the block is active it also publishes ``SANDBOX_ENV_VAR``; that is
    how conftest's autouse ``_isolate_user_paths`` learns an outer sandbox owns
    the redirect and must not be overridden per test.
    """
    import rwmod.config as cfg_mod
    import rwmod.database as db_mod
    import rwmod.deps as deps_mod
    import rwmod.logger as log_mod
    import rwmod.profile as prof_mod
    import rwmod.queue as q_mod
    import rwmod.transfer as transfer_mod
    import rwmod.undo as undo_mod

    home.mkdir(parents=True, exist_ok=True)
    # ``home`` is declared on PurePath (not Path) as of 3.13, so find the class
    # in the MRO that actually owns the descriptor.
    home_owner = next(k for k in Path.__mro__ if "home" in k.__dict__)
    saved_home = home_owner.__dict__["home"]  # the classmethod descriptor itself
    saved = {
        "config_path": cfg_mod.Config.CONFIG_PATH,
        "db_path": db_mod.DB_PATH,
        "log_path": log_mod.LOG_PATH,
        "profiles_dir": prof_mod.PROFILES_DIR,
        # transfer.py does `from rwmod.profile import PROFILES_DIR`, so patching
        # profile alone leaves this second, import-time copy pointing at the
        # real ~/.rwmod/profiles (same trap conftest.py documents).
        "transfer_profiles_dir": transfer_mod.PROFILES_DIR,
        "undo_dir": undo_mod.UNDO_DIR,
        "cache": (cfg_mod.Config._cache, cfg_mod.Config._cache_ts, cfg_mod.Config._cache_path_key),
        "queue": q_mod._queue,
        "autoupdate": deps_mod._autoupdate,
    }

    # Close BEFORE rebinding DB_PATH. rwmod.database caches one connection per
    # thread in ``_local.conn``; get_conn() returns that cached object without
    # re-checking DB_PATH, so a connection opened earlier in the process against
    # the real ~/.rwmod.db would keep serving every later query even after
    # DB_PATH is patched. close_db() drops the cache (and marks the old
    # connections closed so other threads rebuild).
    db_mod.close_db()

    previous_sentinel = os.environ.get(SANDBOX_ENV_VAR)

    Path.home = classmethod(lambda cls: home)
    cfg_mod.Config.CONFIG_PATH = home / ".rwmod.toml"
    db_mod.DB_PATH = home / ".rwmod.db"
    log_mod.LOG_PATH = home / ".rwmod.log"
    prof_mod.PROFILES_DIR = home / ".rwmod" / "profiles"
    transfer_mod.PROFILES_DIR = home / ".rwmod" / "profiles"
    undo_mod.UNDO_DIR = home / ".rwmod" / "undo"
    cfg_mod.Config._cache = None
    cfg_mod.Config._cache_ts = 0.0
    cfg_mod.Config._cache_path_key = None
    q_mod._queue = None
    deps_mod._autoupdate = None
    # Publish the sandbox only once every path it covers is actually live.
    os.environ[SANDBOX_ENV_VAR] = str(home)
    try:
        yield
    finally:
        db_mod.close_db()
        home_owner.home = saved_home
        cfg_mod.Config.CONFIG_PATH = saved["config_path"]
        db_mod.DB_PATH = saved["db_path"]
        log_mod.LOG_PATH = saved["log_path"]
        prof_mod.PROFILES_DIR = saved["profiles_dir"]
        transfer_mod.PROFILES_DIR = saved["transfer_profiles_dir"]
        undo_mod.UNDO_DIR = saved["undo_dir"]
        cfg_mod.Config._cache = saved["cache"][0]
        cfg_mod.Config._cache_ts = saved["cache"][1]
        cfg_mod.Config._cache_path_key = saved["cache"][2]
        q_mod._queue = saved["queue"]
        deps_mod._autoupdate = saved["autoupdate"]
        # Un-publish last: the marker only ever covers a genuinely live sandbox.
        if previous_sentinel is None:
            os.environ.pop(SANDBOX_ENV_VAR, None)
        else:
            os.environ[SANDBOX_ENV_VAR] = previous_sentinel


@pytest.fixture(scope="class")
def e2e(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """Isolated cfg/DB/Mods + a TestClient bound to the real app, shared by the class.

    Class-scoped so the (slow, real) SteamCMD download is performed once and
    the later tests can assert on the resulting installed state.
    """
    mod_id = os.environ.get("RWMOD_E2E_MOD_ID", DEFAULT_MOD_ID)
    _require_opt_in(mod_id)

    tmp = tmp_path_factory.mktemp("e2e")
    home = tmp / "home"
    mods_dir = tmp / "Mods"
    rimworld_dir = tmp / "RimWorld"
    backup_dir = tmp / "backups"
    mods_dir.mkdir()
    rimworld_dir.mkdir()
    backup_dir.mkdir()

    with _isolated_home(home):
        from rwmod.config import Config

        # Refuse to run against anything that looks like the user's install.
        assert "RimWorld" not in str(mods_dir), "mods_dir must not be the game dir"
        assert Path.home() == home

        cfg = Config.load()
        cfg.steamcmd_path = bundle_root() / "steamcmd" / "steamcmd.exe"
        cfg.mods_dir = mods_dir
        cfg.rimworld_dir = rimworld_dir
        cfg.backup_dir = backup_dir
        cfg.save()

        # Re-read through the normal path so the app sees the same config the
        # CLI/settings API would.
        Config._cache = None
        Config._cache_ts = 0.0
        Config._cache_path_key = None
        cfg = Config.load()
        assert cfg.mods_dir == mods_dir
        assert cfg.steamcmd_path == bundle_root() / "steamcmd" / "steamcmd.exe"

        from fastapi.testclient import TestClient

        from rwmod.server import app

        ns = SimpleNamespace(
            cfg=cfg,
            tmp=tmp,
            home=home,
            mods_dir=mods_dir,
            backup_dir=backup_dir,
            mod_id=mod_id,
            pkg_id=os.environ.get("RWMOD_E2E_PKG_ID", DEFAULT_PKG_ID),
            content_dir=cfg.steamcmd_path.parent
            / "steamapps"
            / "workshop"
            / "content"
            / "294100"
            / mod_id,
        )
        try:
            with TestClient(app) as client:
                ns.client = client
                yield ns
        except (asyncio.CancelledError, concurrent.futures.CancelledError) as e:
            # E2E-9: the app's lifespan shutdown raises CancelledError out of
            # queue.wait_stopped() once a queue worker exists (the queue test
            # above starts one). anyio's portal re-raises it as the
            # concurrent.futures flavour, so both classes are caught. Surface it
            # as a warning instead of an opaque fixture ERROR —
            # TestShutdownContract pins the bug precisely.
            warnings.warn(f"app shutdown raised CancelledError (E2E-9): {e!r}", stacklevel=1)
        # Client teardown already closed the DB via lifespan.


def _wait_for_queue(client, mod_id: str, timeout_s: float) -> dict:
    """Poll GET /api/queue until *mod_id* reaches a terminal status."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        items = client.get("/api/queue").json()["items"]
        found = [i for i in items if i["id"] == mod_id]
        if found:
            last = found[0]
            if last["status"] in ("done", "failed", "cancelled"):
                return last
        time.sleep(1.0)
    raise AssertionError(f"queue item {mod_id} never finished; last={last!r}")


class TestE2ERealDownloadChain:
    """E2E-1..E2E-8: the real download chain, in order."""

    def test_isolated_paths_never_point_at_user_data(self, e2e: SimpleNamespace) -> None:
        """E2E-1: the sandbox really is a sandbox — constants AND live connections."""
        import rwmod.database as db_mod
        import rwmod.logger as log_mod
        import rwmod.profile as prof_mod
        import rwmod.undo as undo_mod
        from rwmod.database import get_conn

        assert e2e.cfg.mods_dir == e2e.mods_dir
        assert e2e.cfg.backup_dir == e2e.backup_dir

        # Every home-bound constant must resolve inside the tmp tree.
        for label, p in (
            ("database.DB_PATH", db_mod.DB_PATH),
            ("logger.LOG_PATH", log_mod.LOG_PATH),
            ("profile.PROFILES_DIR", prof_mod.PROFILES_DIR),
            ("undo.UNDO_DIR", undo_mod.UNDO_DIR),
            ("cfg.mods_dir", e2e.cfg.mods_dir),
            ("cfg.backup_dir", e2e.cfg.backup_dir),
        ):
            assert Path(p).is_relative_to(e2e.tmp), f"{label}={p} escaped the sandbox"

        # The real game dir must not appear anywhere in the resolved config.
        blob = " ".join(
            str(p) for p in (e2e.cfg.mods_dir, e2e.cfg.rimworld_dir, e2e.cfg.backup_dir)
        ).lower()
        assert "d:/rimworld" not in blob.replace("\\", "/")

        # ...and the connection the app is actually serving from is the sandbox
        # file, not a real-home connection cached before DB_PATH was rebound.
        live = get_conn().execute("PRAGMA database_list").fetchall()
        opened = [r["file"] for r in live if r["file"]]
        assert opened, "no on-disk database attached"
        for f in opened:
            assert Path(f).is_relative_to(e2e.tmp), f"live DB connection points at {f}"

    def test_frontend_index_is_served(self, e2e: SimpleNamespace) -> None:
        """E2E-2: GET / serves the built SPA (503 means the frontend is unbuilt)."""
        r = e2e.client.get("/")
        assert r.status_code == 200, (
            "GET / returned 503 — build the frontend first "
            "(`python tools/build_frontend.py`), then re-run"
        )
        assert "text/html" in r.headers["content-type"]
        assert "<html" in r.text.lower()

    def test_status_and_docs_are_reachable(self, e2e: SimpleNamespace) -> None:
        """E2E-3: /api/status and the OpenAPI docs are live on the isolated instance."""
        status = e2e.client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["online"] is True

        assert e2e.client.get("/api/docs").status_code == 200

        spec = e2e.client.get("/api/openapi.json").json()
        assert "/api/download" in spec["paths"]
        assert spec["info"]["version"]

    def test_real_download_installs_mod(self, e2e: SimpleNamespace) -> None:
        """E2E-4: a real SteamCMD download lands in the isolated Mods dir."""
        from rwmod.database import get_download_history

        # The cache must hold the payload SteamCMD is about to validate.
        assert (e2e.content_dir / "About" / "About.xml").exists()

        t0 = time.monotonic()
        r = e2e.client.post("/api/download", json={"ids": [e2e.mod_id], "force": False})
        elapsed = time.monotonic() - t0
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["results"] == [{"id": e2e.mod_id, "ok": True}]
        # A cold start pays SteamCMD's process launch + anonymous login.
        assert elapsed < _ROUND_TRIP_TIMEOUT_S

        installed = e2e.mods_dir / e2e.pkg_id
        assert installed.is_dir(), f"expected {installed} to exist"
        about = installed / "About" / "About.xml"
        assert about.is_file()
        assert (installed / "About" / "PublishedFileId.txt").read_text(
            encoding="utf-8"
        ).strip() == e2e.mod_id

        # About.xml must actually parse through the shared safe reader.
        from rwmod.metadata import read_mod_metadata

        meta = read_mod_metadata(installed)
        assert meta is not None
        assert meta.package_id == e2e.pkg_id
        assert meta.workshop_id == e2e.mod_id

        # download_history status machine got a success row for this id.
        rows = [h for h in get_download_history(limit=50) if h["workshop_id"] == e2e.mod_id]
        assert rows, "no download_history row written"
        assert rows[0]["status"] == "success"

    def test_reinstall_without_force_skips(self, e2e: SimpleNamespace) -> None:
        """E2E-5: a second non-force download sees the mod installed and skips."""
        from rwmod.downloader import _find_existing

        # The metadata index must resolve the folder without a directory scan.
        assert _find_existing(e2e.mods_dir, e2e.mod_id) == e2e.mods_dir / e2e.pkg_id

        t0 = time.monotonic()
        r = e2e.client.post("/api/download", json={"ids": [e2e.mod_id], "force": False})
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert r.json()["results"] == [{"id": e2e.mod_id, "ok": True}]
        # No SteamCMD process is launched on the skip path.
        assert elapsed < 5.0, f"skip path took {elapsed:.1f}s — did it re-download?"

    def test_force_download_replaces_the_installed_folder(self, e2e: SimpleNamespace) -> None:
        """E2E-6: force overwrite rebuilds the folder and leaves no stash behind."""
        installed = e2e.mods_dir / e2e.pkg_id
        marker = installed / "PROOF_MARKER.txt"
        marker.write_text("planted by test_probe_e2e\n", encoding="utf-8")

        r = e2e.client.post("/api/download", json={"ids": [e2e.mod_id], "force": True})
        assert r.status_code == 200
        assert r.json()["results"] == [{"id": e2e.mod_id, "ok": True}]

        assert not marker.exists(), "force overwrite did not replace the folder contents"
        assert (installed / "About" / "About.xml").is_file()
        # The stash used for rollback must be cleaned up on success.
        assert not list(e2e.mods_dir.glob(".rwmod_stash_*"))

    def test_backup_list_and_restore_roundtrip(self, e2e: SimpleNamespace) -> None:
        """E2E-7: backup zip -> delete -> list -> restore brings the exact bytes back."""
        installed = e2e.mods_dir / e2e.pkg_id
        proof = installed / "RESTORE_PROOF.txt"
        proof.write_text("restored-by-test\n", encoding="utf-8")

        # batch/delete zips the folder into backup_dir before removing it.
        r = e2e.client.post("/api/mods/batch/delete", json={"folders": [e2e.pkg_id]})
        assert r.status_code == 200
        assert r.json()["deleted"] == [e2e.pkg_id]
        assert not installed.exists()

        zips = sorted(e2e.backup_dir.glob(f"{e2e.mod_id}__*.zip"))
        assert zips, f"no backup zip in {e2e.backup_dir}"

        listing = e2e.client.get(f"/api/backups?workshop_id={e2e.mod_id}").json()
        assert listing["backup_dir"] == str(e2e.backup_dir)
        assert [b["filename"] for b in listing["backups"]] == [z.name for z in zips]

        r = e2e.client.post(f"/api/backups/{e2e.mod_id}/restore", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["restored_folder"] == e2e.pkg_id

        assert proof.read_text(encoding="utf-8") == "restored-by-test\n"
        assert (installed / "About" / "About.xml").is_file()
        # Atomic-swap temporaries must not survive.
        assert not list(e2e.mods_dir.glob(".rwmod_restore_*"))
        assert not list(e2e.mods_dir.glob(".rwmod_old_*"))

        # The restored mod is still a first-class citizen to /api/mods.
        mods = {m["folder"]: m for m in e2e.client.get("/api/mods").json()}
        assert mods[e2e.pkg_id]["workshop_id"] == e2e.mod_id
        assert mods[e2e.pkg_id]["package_id"] == e2e.pkg_id

    def test_queue_drives_a_real_download(self, e2e: SimpleNamespace) -> None:
        """E2E-8: the queue state machine (pending->downloading->done) persists to SQLite.

        Uses a second cached, dependency-free id so the queue actually has work
        to do (a queued already-installed id short-circuits to ``done``).
        """
        from rwmod.database import get_conn

        other = os.environ.get("RWMOD_E2E_MOD_ID_2", "3457861400")
        if not (
            bundle_root() / "steamcmd" / "steamapps" / "workshop" / "content" / "294100" / other
        ).exists():
            pytest.skip(f"second cached mod {other} not available")

        r = e2e.client.post("/api/queue/add", json={"ids": [other]})
        assert r.status_code == 200
        assert r.json()["added"] == 1

        pending = [i for i in e2e.client.get("/api/queue").json()["items"] if i["id"] == other]
        assert pending and pending[0]["status"] == "pending"
        row = (
            get_conn()
            .execute("SELECT status FROM download_queue WHERE workshop_id = ?", (other,))
            .fetchone()
        )
        assert row is not None and row["status"] == "pending"

        assert e2e.client.post("/api/queue/start", json={"force": False}).status_code == 200
        final = _wait_for_queue(e2e.client, other, _ROUND_TRIP_TIMEOUT_S)
        assert final["status"] == "done", final
        assert final["progress"] == 1.0
        assert final["name"], "queue item name was never populated from the installed folder"

        row = (
            get_conn()
            .execute(
                "SELECT status, progress, name FROM download_queue WHERE workshop_id = ?", (other,)
            )
            .fetchone()
        )
        assert row["status"] == "done"
        assert row["progress"] == 1.0
        assert (e2e.mods_dir / row["name"]).is_dir()


class TestShutdownContract:
    """E2E-9: shutdown must survive a started queue worker.

    Observed while tearing down the class fixture above: ``TestClient.__exit__``
    failed with ``CancelledError`` from ``wait_stopped``, and a real
    ``uvicorn.Server.run()`` logs ``Application shutdown failed. Exiting.``.

    No network is required for this repro (it is marked network only because
    every case in this module is); it lives here because this is where the
    chain surfaced it.
    """

    def test_wait_stopped_returns_after_stop(self, tmp_path: Path) -> None:
        from rwmod.config import Config
        from rwmod.queue import DownloadQueue

        cfg = Config(
            mods_dir=tmp_path / "Mods",
            rimworld_dir=tmp_path / "RimWorld",
            backup_dir=tmp_path / "backups",
        )

        async def scenario() -> None:
            q = DownloadQueue()
            await q.start(cfg, force=False)  # creates the persistent worker task
            await asyncio.sleep(0.1)  # let it reach the idle wait
            q.stop()  # worker.cancel()
            await q.wait_stopped(timeout=1.0)  # must return, not raise

        asyncio.run(scenario())
