"""BE-FIX wave 4 — regressions for the independent-review findings.

Backend / packaging items assigned to BE-FIX:

* REV-001 cancellation is scoped per ``(instance_id, workshop id)`` end to end;
* REV-002 collection children re-queued by a forced batch inherit ``force``;
* REV-003 a corrupt ModsConfig.xml is a 400, never a 200 with zero mods;
* REV-005 the Docker frontend stage can read the real version (not 0.0.0);
* REV-006 queue-remove / tag mutations fail with 404/409, not a 200 ok:false;
* REV-010 ModsConfig / profile / undo temp files are unique per writer;
* REV-013 the uninstaller removes the SteamCMD ``steamapps`` cache only;
* REV-015 "clear completed" also clears rows persisted by an earlier session;
* REV-016 retrying failed downloads is scoped to one instance;
* REV-017 the gate's version check no longer imports ``rwmod.server``;
* REV-018 bundle imports use ``tempfile.mkdtemp`` instead of a second-stamped name.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rwmod.config import Config
from rwmod.queue import DownloadQueue, QueueItem

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path: Path) -> Config:
    exe = tmp_path / "steamcmd" / "steamcmd.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.touch()
    cfg = Config(steamcmd_path=exe, mods_dir=tmp_path / "Mods")
    cfg.mods_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir = tmp_path / "Backups"
    return cfg


# ── REV-001: cancellation is scoped per instance ───────────────────


class _BlockingProc:
    """Popen double whose wait() blocks until kill()/signal() is delivered."""

    pid = 4242

    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self._done = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self._done.wait(timeout)
        return 0

    def kill(self) -> None:
        self._done.set()

    def send_signal(self, _sig: object) -> None:
        self._done.set()

    def terminate(self) -> None:
        self._done.set()


class _FakeReader:
    def __init__(self, stdout: object, ids: set[str], progress_cb: object) -> None:
        self.lines: list[str] = []
        self.completed: set[str] = set()

    def start(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        pass


@pytest.fixture
def clean_proc_registry():
    from rwmod.steamcmd_support import procs

    def _clean() -> None:
        with procs._live_guard:
            procs._live_procs.clear()
            procs._cancelled_procs.clear()
            procs._cancel_requests.clear()

    _clean()
    yield procs
    _clean()


class TestInstanceScopedCancellation:
    def test_cancel_kills_only_the_named_instance(self, clean_proc_registry):
        procs = clean_proc_registry
        alpha, beta = MagicMock(), MagicMock()
        with procs._live_guard:
            procs._live_procs[("alpha", "111")] = alpha
            procs._live_procs[("beta", "111")] = beta

        assert procs.cancel_download("alpha", "111") is True
        assert alpha.kill.called
        assert not beta.kill.called
        assert ("alpha", "111") not in procs._live_procs
        assert ("beta", "111") in procs._live_procs

    def test_pending_cancel_request_is_instance_scoped(self, clean_proc_registry):
        procs = clean_proc_registry
        assert procs.request_cancel("alpha", "111") is False  # nothing live yet
        assert procs._consume_cancel_requests("beta", ["111"]) == set()
        assert procs._consume_cancel_requests("alpha", ["111"]) == {"111"}

    def test_cancelling_another_instance_leaves_the_batch_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_proc_registry
    ):
        from rwmod import steamcmd as sc

        procs = clean_proc_registry
        proc = _BlockingProc()
        monkeypatch.setattr("rwmod.steamcmd.subprocess.Popen", lambda *_a, **_k: proc)
        monkeypatch.setattr("rwmod.steamcmd._ProgressReader", _FakeReader)

        exe = tmp_path / "steamcmd.exe"
        results: dict = {}
        worker = threading.Thread(
            target=lambda: results.update(
                sc.SteamCMD(exe).workshop_download_many(["111"], instance_id="alpha")
            )
        )
        worker.start()
        for _ in range(400):
            with procs._live_guard:
                if ("alpha", "111") in procs._live_procs:
                    break
            time.sleep(0.005)
        assert ("alpha", "111") in procs._live_procs

        # beta's download is a different row/process — cancelling it must be a
        # no-op and must not terminate alpha's batch.
        assert sc.cancel_download("beta", "111") is False
        assert worker.is_alive()

        assert sc.cancel_download("alpha", "111") is True
        worker.join(5)
        assert not worker.is_alive()
        assert results["111"].error_kind == sc.ErrorKind.CANCELLED


# ── REV-002: collection children inherit the batch's force intent ───


class TestCollectionChildrenInheritForce:
    @staticmethod
    def _run(tmp_path: Path, force: bool) -> DownloadQueue:
        cfg = _cfg(tmp_path)
        q = DownloadQueue()
        item = QueueItem(id="A", force=force)
        q.items.append(item)

        def fake_batch(config, ids, force=False, extra_out=None, progress_cb=None):
            if extra_out is not None:
                extra_out.append("B")
            return {mid: True for mid in ids}

        with patch("rwmod.queue.download_batch", side_effect=fake_batch):
            asyncio.run(q._process_batch(cfg, [item], force=force))
        return q

    def test_forced_batch_marks_children_forced(self, tmp_path: Path):
        q = self._run(tmp_path, force=True)
        child = next(i for i in q.items if i.id == "B")
        assert child.force is True

    def test_plain_batch_children_stay_non_forced(self, tmp_path: Path):
        q = self._run(tmp_path, force=False)
        child = next(i for i in q.items if i.id == "B")
        assert child.force is False


# ── REV-003: corrupt ModsConfig.xml is a 400 ───────────────────────


class TestCorruptModsConfigIsAnError:
    def test_parser_raises_validation_error(self, tmp_path: Path):
        from rwmod.errors import ValidationError
        from rwmod.parser import parse_mods_config

        broken = tmp_path / "ModsConfig.xml"
        broken.write_text("<<<not xml>>>", encoding="utf-8")
        with pytest.raises(ValidationError):
            parse_mods_config(broken)

    def test_import_sort_corrupt_xml_answers_400(self, client):
        resp = client.post(
            "/api/import/sort",
            files={"file": ("ModsConfig.xml", b"<<<not xml>>>", "text/xml")},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"] == "ValidationError"
        assert body["detail"]


# ── REV-005: Docker frontend stage can read the version ────────────


class TestDockerVersionSource:
    def test_frontend_stage_sees_pyproject_and_asserts_it(self):
        text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        stage1 = text.split("# ── Stage 2", 1)[0]
        copy_at = stage1.index("COPY pyproject.toml /app/pyproject.toml")
        build_at = stage1.index("npx vite build")
        assert copy_at < build_at, "pyproject.toml must exist before vite reads it"
        # The build refuses to fall back to the 0.0.0 placeholder.
        assert "0.0.0" in stage1
        assert "process.exit(1)" in stage1

    def test_dockerignore_does_not_exclude_pyproject(self):
        lines = [
            line.strip()
            for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert "pyproject.toml" not in lines


# ── REV-006: failed mutations are 404/409, not 200 ok:false ─────────


class TestMutationFailureContract:
    def test_queue_remove_missing_is_404(self, client):
        resp = client.delete("/api/queue/123456")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "ModNotFoundError"

    def test_queue_remove_existing_is_200_data(self, client):
        assert client.post("/api/queue/add", json={"ids": ["123456"]}).status_code == 200
        resp = client.delete("/api/queue/123456")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}

    def test_duplicate_tag_is_409(self, client):
        assert client.post("/api/tags/f1", json={"tag": "qol"}).status_code == 200
        resp = client.post("/api/tags/f1", json={"tag": "qol"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"] == "ConflictError"

    def test_removing_a_missing_tag_is_404(self, client):
        resp = client.delete("/api/tags/f1/missing")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "ModNotFoundError"


# ── REV-010: unique temp files per writer ──────────────────────────


class TestUniqueTempFiles:
    def test_parallel_profile_saves_use_distinct_temp_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.profile as profile_mod

        monkeypatch.setattr(profile_mod, "PROFILES_DIR", tmp_path / "profiles")
        seen: list[str] = []
        lock = threading.Lock()

        def spy(src: object, _dst: object) -> None:
            # Record the scratch file name only. Two simultaneous os.replace()
            # calls onto the *same* destination are an independent OS-level
            # race on Windows; REV-010 is specifically about the scratch file
            # two writers shared before. save_profile() unlinks the temp in its
            # own finally block, so nothing leaks.
            with lock:
                seen.append(Path(src).name)

        monkeypatch.setattr(profile_mod.os, "replace", spy)

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def save(i: int) -> None:
            barrier.wait(5)
            try:
                profile_mod.save_profile(
                    "p",
                    f"<ModsConfigData><v>{i}</v></ModsConfigData>",
                    instance_id="default",
                )
            except Exception as e:  # noqa: BLE001 — asserted below
                errors.append(e)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert errors == []
        assert len(seen) == 2
        assert len(set(seen)) == 2, "two writers shared one temp file"

    def test_undo_atomic_copy_uses_a_distinct_temp_name_per_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.undo as undo_mod

        seen: list[str] = []
        real_replace = undo_mod.os.replace

        def spy(src: object, dst: object) -> None:
            seen.append(Path(src).name)
            real_replace(src, dst)

        monkeypatch.setattr(undo_mod.os, "replace", spy)

        for i in range(2):
            src = tmp_path / f"src{i}.xml"
            src.write_text(f"<v>{i}</v>", encoding="utf-8")
            target = tmp_path / f"dst{i}.xml"
            undo_mod._atomic_copy(src, target)
            assert target.read_text(encoding="utf-8") == f"<v>{i}</v>"

        assert len(seen) == 2
        assert len(set(seen)) == 2

    def test_rimsort_apply_uses_the_shared_atomic_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.modsconfig_edit as mce
        from rwmod.routers.rimsort import rimsort_apply

        cfg = _cfg(tmp_path)
        config_dir = tmp_path / "explicit_config"
        config_dir.mkdir()
        cfg.config_dir = config_dir
        cfg.save()
        target = config_dir / "ModsConfig.xml"
        target.write_text(
            "<ModsConfigData><activeMods><li>b.mod</li><li>a.mod</li></activeMods>"
            "</ModsConfigData>",
            encoding="utf-8",
        )
        for folder, pkg in (("A", "a.mod"), ("B", "b.mod")):
            d = cfg.mods_dir / folder
            (d / "About").mkdir(parents=True, exist_ok=True)
            (d / "About" / "About.xml").write_text(
                f"<ModMetaData><name>{folder}</name><packageId>{pkg}</packageId></ModMetaData>",
                encoding="utf-8",
            )

        calls: list[tuple[Path, str]] = []
        real_writer = mce.write_modsconfig_target

        def spy(path: Path, text: str, *, label: str = "batch-active"):
            calls.append((Path(path), label))
            return real_writer(path, text, label=label)

        monkeypatch.setattr(mce, "write_modsconfig_target", spy)

        body = rimsort_apply(cfg=cfg)

        assert len(calls) == 1
        assert calls[0] == (target, "sort")
        assert body["backup"].startswith("ModsConfig.xml.bak_")
        assert not list(config_dir.glob("*.rwmod.tmp"))


# ── REV-013: uninstaller cleans the SteamCMD cache ─────────────────


class TestUninstallerCleanup:
    def test_uninstall_deletes_steamapps_but_not_user_mods(self):
        text = (REPO_ROOT / "rwmod.iss").read_text(encoding="utf-8")
        assert "[UninstallDelete]" in text
        section = text.split("[UninstallDelete]", 1)[1]
        assert 'Type: filesandordirs; Name: "{app}\\steamcmd\\steamapps"' in section
        # Only the SteamCMD cache may be removed — never the user's Mods dir.
        entries = [line for line in section.splitlines() if line.strip().startswith("Type:")]
        assert entries
        assert not any("{app}\\mods" in line.lower() for line in entries)


# ── REV-015: clear-done also clears persisted finished rows ────────


class TestClearDoneAfterRestart:
    def test_persisted_finished_rows_are_cleared_without_memory_items(self):
        from rwmod.database import init_db, queue_load_all, queue_upsert

        init_db()
        queue_upsert("111", instance_id="alpha", status="done")
        queue_upsert("222", instance_id="beta", status="cancelled")
        queue_upsert("333", instance_id="alpha", status="pending")

        q = DownloadQueue()
        q._load_from_db()
        # Startup restores pending/downloading only: the finished rows are not
        # in memory, which is exactly why the old clear_done() missed them.
        assert [i.id for i in q.items] == ["333"]

        q.clear_done()

        remaining = {(r["instance_id"], r["workshop_id"]) for r in queue_load_all()}
        assert remaining == {("alpha", "333")}
        assert [i.id for i in q.items] == ["333"]


# ── REV-016: retry is scoped to one instance ───────────────────────


class TestRetryIsInstanceScoped:
    def test_retrying_alpha_does_not_requeue_beta(self):
        q = DownloadQueue()
        q.add(["111"], instance_id="alpha")
        q.add(["111"], instance_id="beta")
        for item in q.items:
            item.status = "failed"

        assert q.retry_failed(["111"], instance_id="alpha") == 1

        assert {i.instance_id: i.status for i in q.items} == {
            "alpha": "pending",
            "beta": "failed",
        }

    def test_api_retry_takes_an_instance_query_parameter(self, client):
        from unittest.mock import AsyncMock, patch

        from rwmod.queue import get_queue

        q = get_queue()
        q.add(["111"], instance_id="alpha")
        q.add(["111"], instance_id="beta")
        for item in q.items:
            item.status = "failed"

        with patch.object(q, "start", new=AsyncMock()) as start:
            resp = client.post("/api/queue/retry?instance_id=alpha", json={"ids": ["111"]})

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"accepted": 1}
        assert {i.instance_id: i.status for i in q.items} == {
            "alpha": "pending",
            "beta": "failed",
        }
        start.assert_awaited_once()


# ── REV-017: the gate's version check has no import side effects ───


class TestGateVersionCheck:
    def test_run_tests_version_check_does_not_import_server(self):
        src = (REPO_ROOT / "run_tests.py").read_text(encoding="utf-8")
        assert "from rwmod.server import" not in src
        assert "tomllib" in src
        assert "AppVersion" in src


# ── REV-018: bundle import scratch dir is unique ───────────────────


class TestImportTempDir:
    def test_consecutive_imports_use_distinct_scratch_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.transfer as transfer_mod

        cfg = _cfg(tmp_path)
        bundle = tmp_path / "probe.rwmod"
        transfer_mod.export_bundle(cfg, bundle, include_backups=False)

        created: list[Path] = []
        real_mkdtemp = transfer_mod.tempfile.mkdtemp

        def spy(*args: object, **kwargs: object) -> str:
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        monkeypatch.setattr(transfer_mod.tempfile, "mkdtemp", spy)

        first = transfer_mod.import_bundle(cfg, bundle)
        second = transfer_mod.import_bundle(cfg, bundle)

        assert first["ok"] is True, first
        assert second["ok"] is True, second
        assert len(created) == 2
        assert created[0] != created[1]
        assert not any(p.exists() for p in created), "scratch dir not cleaned up"
