"""Unit tests for mod_health_check.py — persistent, non-blocking health cache.

Old behavior: /api/mods/health performed 6 Steam API batches synchronously
(10-16s typical, up to 45s) on every cold request, and /api/dashboard blocked
on the same call. New contract: mod_cache rows (6h TTL) answer immediately; a
daemon thread refreshes behind a single-flight guard; a failed refresh keeps
the last known status and never mass-marks mods as removed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import rwmod.mod_health_check as mod_health_check


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Isolated DB + Mods dir; never the real ~/.rwmod.db."""
    import rwmod.database as db_mod
    import rwmod.mod_cache as cache_mod

    db_mod.close_db()
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "health.db")
    monkeypatch.setattr(db_mod, "_initialized", False)
    db_mod.init_db()
    cache_mod.invalidate_cache()

    mods = tmp_path / "Mods"
    mods.mkdir()
    ns = SimpleNamespace(tmp=tmp_path, mods=mods, db=db_mod, cache=cache_mod)
    yield ns

    mod_health_check.stop_all()
    cache_mod.invalidate_cache()
    db_mod.close_db()


def _make_mod(mods: Path, folder: str, workshop_id: str) -> Path:
    about = mods / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{folder}</name>"
        f"<packageId>x.{folder.lower()}</packageId></ModMetaData>",
        encoding="utf-8",
    )
    (about / "PublishedFileId.txt").write_text(workshop_id, encoding="utf-8")
    return mods / folder


def _warm_metadata(env: SimpleNamespace) -> None:
    env.cache.invalidate_cache()
    env.cache.get_cached_mods(env.mods)


def _seed(env: SimpleNamespace, workshop_id: str, time_updated: int, age_hours: int = 0) -> None:
    db = env.db.get_conn()
    db.execute(
        "INSERT INTO mod_cache (workshop_id, time_updated, cached_at) "
        "VALUES (?, ?, datetime('now', ?))",
        (workshop_id, time_updated, f"-{age_hours} hours"),
    )
    db.commit()


def _row(env: SimpleNamespace, workshop_id: str) -> dict:
    cur = env.db.get_conn().execute(
        "SELECT workshop_id, time_updated, cached_at FROM mod_cache WHERE workshop_id = ?",
        (workshop_id,),
    )
    return dict(cur.fetchone())


def _status_map(result: dict) -> dict[str, str]:
    return {m["folder"]: m["status"] for m in result["mods"]}


class TestCacheHit:
    def test_fresh_cache_answers_without_network(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_mod(env.mods, "Fresh", "1")
        _make_mod(env.mods, "Old", "2")
        _warm_metadata(env)
        now = int(time.time())
        _seed(env, "1", now - 5 * 86400)
        _seed(env, "2", now - 400 * 86400)

        def boom(_ids: list[str]) -> dict:
            raise AssertionError("network must not be called on a fresh cache hit")

        monkeypatch.setattr(mod_health_check, "_fetch_batch", boom)

        result = mod_health_check.get_health(env.mods)

        assert result["stale"] is False
        assert result["refreshing"] is False
        assert _status_map(result) == {"Fresh": "maintained", "Old": "abandoned"}
        assert next(m for m in result["mods"] if m["folder"] == "Fresh")["last_updated"]

    def test_empty_mods_dir(self, env: SimpleNamespace) -> None:
        assert mod_health_check.get_health(env.mods) == {
            "mods": [],
            "stale": False,
            "refreshing": False,
        }


class TestFailureKeepsOldState:
    def test_stale_cache_keeps_last_status_when_network_fails(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_mod(env.mods, "Kept", "1")
        _warm_metadata(env)
        seeded = int(time.time()) - 5 * 86400
        _seed(env, "1", seeded, age_hours=7)
        before = _row(env, "1")

        monkeypatch.setattr(mod_health_check, "_fetch_batch", lambda _ids: {})

        result = mod_health_check.get_health(env.mods)
        assert result["stale"] is True
        assert result["refreshing"] is True
        assert mod_health_check.wait_for_refresh(env.mods, timeout=5.0) is True

        # The failed refresh must not touch the cache: status stays maintained,
        # never "removed" or "unknown".
        result = mod_health_check.get_health(env.mods)
        assert _status_map(result) == {"Kept": "maintained"}
        after = _row(env, "1")
        assert after["time_updated"] == seeded
        assert after["cached_at"] == before["cached_at"]

    def test_explicit_refresh_failure_returns_false(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_mod(env.mods, "A", "1")
        _make_mod(env.mods, "B", "2")
        _warm_metadata(env)
        monkeypatch.setattr(mod_health_check, "_fetch_batch", lambda _ids: {})
        assert mod_health_check.refresh_now(env.mods) is False
        assert env.db.get_conn().execute("SELECT COUNT(*) AS n FROM mod_cache").fetchone()["n"] == 0


class TestSuccessfulRefresh:
    def test_persists_details_and_marks_absent_ids_removed(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_mod(env.mods, "Here", "1")
        _make_mod(env.mods, "Gone", "2")
        _warm_metadata(env)
        now = int(time.time())
        seen: list[list[str]] = []

        def fake_fetch(ids: list[str]) -> dict:
            seen.append(list(ids))
            return {"1": {"time_updated": now - 5 * 86400}} if "1" in ids else {}

        monkeypatch.setattr(mod_health_check, "_fetch_batch", fake_fetch)

        assert mod_health_check.refresh_now(env.mods) is True
        assert len(seen) == 1, "fewer than 100 ids must stay in one batch"
        assert sorted(seen[0]) == ["1", "2"]

        assert _row(env, "1")["time_updated"] == now - 5 * 86400
        assert _row(env, "2")["time_updated"] == -1

        result = mod_health_check.get_health(env.mods)
        assert result["stale"] is False
        assert _status_map(result) == {"Here": "maintained", "Gone": "removed"}

    def test_fresh_cache_reports_stale_false_and_does_not_refresh(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_mod(env.mods, "Fresh", "1")
        _warm_metadata(env)
        _seed(env, "1", int(time.time()) - 5 * 86400, age_hours=1)
        monkeypatch.setattr(
            mod_health_check,
            "_fetch_batch",
            lambda _ids: (_ for _ in ()).throw(AssertionError("unexpected refresh")),
        )
        result = mod_health_check.get_health(env.mods)
        assert result["stale"] is False
        assert result["refreshing"] is False
        assert _status_map(result) == {"Fresh": "maintained"}


class TestSingleFlight:
    def test_two_refresh_calls_share_one_network_pass(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_mod(env.mods, "One", "1")
        _warm_metadata(env)
        calls = {"n": 0}
        started = threading.Event()
        release = threading.Event()

        def blocking_fetch(_ids: list[str]) -> dict:
            calls["n"] += 1
            started.set()
            assert release.wait(5.0)
            return {"1": {"time_updated": int(time.time())}}

        monkeypatch.setattr(mod_health_check, "_fetch_batch", blocking_fetch)

        assert mod_health_check.refresh_async(env.mods) is True
        assert started.wait(2.0) is True
        assert mod_health_check.refresh_async(env.mods) is False

        release.set()
        assert mod_health_check.wait_for_refresh(env.mods, timeout=5.0) is True
        assert calls["n"] == 1


class TestDashboardIntegration:
    def test_dashboard_reads_cached_health_without_refreshing(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dashboard request must never trigger the Steam health refresh."""
        import rwmod.database as db_mod
        import rwmod.mod_cache as cache_mod
        import rwmod.mod_sizes as mod_sizes
        from rwmod.config import Config

        cfg = Config.load()
        _make_mod(cfg.mods_dir, "Old", "1")
        cache_mod.invalidate_cache()
        cache_mod.get_cached_mods(cfg.mods_dir)

        db = db_mod.get_conn()
        db.execute(
            "INSERT INTO mod_cache (workshop_id, time_updated, cached_at) "
            "VALUES (?, ?, datetime('now'))",
            ("1", int(time.time()) - 400 * 86400),
        )
        db.commit()

        calls: list[list[str]] = []

        def tracking_fetch(ids: list[str]) -> dict:
            calls.append(list(ids))
            return {}

        monkeypatch.setattr(mod_health_check, "_fetch_batch", tracking_fetch)
        try:
            resp = client.get("/api/dashboard")
            assert resp.status_code == 200
            body = resp.json()
            assert body["health"]["abandoned"] == 1
            assert calls == [], "dashboard must not start a health refresh"
        finally:
            mod_sizes.stop_all()
            mod_sizes.invalidate()
            cache_mod.invalidate_cache()
