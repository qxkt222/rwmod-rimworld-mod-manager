"""Test SteamCMD error parsing — the core of error handling logic.

Tests the _parse_workshop_error and _classify_reason logic
without needing actual SteamCMD installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rwmod.steamcmd import ErrorKind, SteamCMD, _decide_download_result


@pytest.fixture
def steamcmd(tmp_path: Path):
    """Create a SteamCMD instance pointing at a temp directory."""
    exe = tmp_path / "steamcmd.exe"
    exe.touch()
    return SteamCMD(exe)


class TestClassifyReason:
    def test_ok(self):
        assert SteamCMD._classify_reason("OK") == ErrorKind.OK
        assert SteamCMD._classify_reason("Ok") == ErrorKind.OK
        # _classify_reason does .lower() first — case-insensitive
        assert SteamCMD._classify_reason("ok") == ErrorKind.OK

    def test_failure(self):
        assert SteamCMD._classify_reason("Failure") == ErrorKind.FAILURE

    def test_file_not_found(self):
        assert SteamCMD._classify_reason("File Not Found") == ErrorKind.FILE_NOT_FOUND

    def test_access_denied(self):
        assert SteamCMD._classify_reason("Access Denied") == ErrorKind.ACCESS_DENIED

    def test_no_match(self):
        assert SteamCMD._classify_reason("No Match") == ErrorKind.NO_MATCH

    def test_timeout(self):
        # Note: TIMEOUT is set by the subprocess exception handler,
        # NOT by _classify_reason (which has no timeout branch).
        # _classify_reason returns UNKNOWN for unrecognized strings.
        assert SteamCMD._classify_reason("Download timed out") == ErrorKind.UNKNOWN

    def test_unknown(self):
        assert SteamCMD._classify_reason("SomeWeirdError") == ErrorKind.UNKNOWN
        assert SteamCMD._classify_reason("") == ErrorKind.UNKNOWN


class TestDecideDownloadResult:
    """The log verdict is authoritative; stale on-disk content must not mask it."""

    def test_explicit_failure_with_content_is_still_failure(self):
        result = _decide_download_result(
            "123", ErrorKind.FAILURE, "SteamCMD: Failure", content_exists=True, lines=["out"]
        )
        assert result.success is False
        assert result.error_kind == ErrorKind.FAILURE
        assert result.error_detail == "SteamCMD: Failure"
        assert result.output_lines == ["out"]

    def test_file_not_found_with_content_is_still_failure(self):
        result = _decide_download_result(
            "123",
            ErrorKind.FILE_NOT_FOUND,
            "SteamCMD: File Not Found",
            content_exists=True,
            lines=[],
        )
        assert result.success is False
        assert result.error_kind == ErrorKind.FILE_NOT_FOUND
        assert result.error_detail == "SteamCMD: File Not Found"

    def test_unknown_with_content_trusts_filesystem(self):
        result = _decide_download_result(
            "123", ErrorKind.UNKNOWN, "no log line", content_exists=True, lines=[]
        )
        assert result.success is True
        assert result.error_kind == ErrorKind.OK
        assert result.error_detail == ""

    def test_ok_without_content_is_failure(self):
        result = _decide_download_result("123", ErrorKind.OK, "", content_exists=False, lines=[])
        assert result.success is False
        assert result.error_kind == ErrorKind.FAILURE
        assert result.error_detail == "SteamCMD 报告下载成功，但磁盘上找不到下载内容"

    def test_ok_with_content_is_success(self):
        result = _decide_download_result("123", ErrorKind.OK, "", content_exists=True, lines=[])
        assert result.success is True
        assert result.error_kind == ErrorKind.OK
        assert result.error_detail == ""


class TestErrorKind:
    def test_explain(self):
        assert "下载成功" in ErrorKind.explain(ErrorKind.OK)
        assert "文件不可用" in ErrorKind.explain(ErrorKind.FAILURE)
        assert "已从创意工坊移除" in ErrorKind.explain(ErrorKind.FILE_NOT_FOUND)
        assert "私密" in ErrorKind.explain(ErrorKind.ACCESS_DENIED)
        assert "不是 RimWorld" in ErrorKind.explain(ErrorKind.NO_MATCH)
        assert "超时" in ErrorKind.explain(ErrorKind.TIMEOUT)
        assert "未知" in ErrorKind.explain(ErrorKind.UNKNOWN)

    def test_non_retryable(self):
        assert ErrorKind.FAILURE in ErrorKind.NON_RETRYABLE
        assert ErrorKind.FILE_NOT_FOUND in ErrorKind.NON_RETRYABLE
        assert ErrorKind.ACCESS_DENIED in ErrorKind.NON_RETRYABLE
        assert ErrorKind.NO_MATCH in ErrorKind.NON_RETRYABLE
        assert ErrorKind.ENVIRONMENT in ErrorKind.NON_RETRYABLE
        assert ErrorKind.OK not in ErrorKind.NON_RETRYABLE
        assert ErrorKind.TIMEOUT not in ErrorKind.NON_RETRYABLE
        assert ErrorKind.UNKNOWN not in ErrorKind.NON_RETRYABLE

    def test_environment_explain(self):
        assert "非 ASCII" in ErrorKind.explain(ErrorKind.ENVIRONMENT)


class TestParseWorkshopError:
    def test_no_log_file(self, steamcmd: SteamCMD):
        kind, detail = steamcmd._parse_workshop_error("12345")
        assert kind == ErrorKind.UNKNOWN
        assert "workshop_log.txt 不存在" in detail

    def test_ok_result(self, steamcmd: SteamCMD, tmp_path: Path):
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[AppID 294100] Download item 12345 result : OK\n")
        kind, _ = steamcmd._parse_workshop_error("12345")
        assert kind == ErrorKind.OK

    def test_failure_result(self, steamcmd: SteamCMD, tmp_path: Path):
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[AppID 294100] Download item 12345 result : Failure\n")
        kind, _ = steamcmd._parse_workshop_error("12345")
        assert kind == ErrorKind.FAILURE

    def test_file_not_found_result(self, steamcmd: SteamCMD, tmp_path: Path):
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "[AppID 294100] Get details for item 12345 failed : File Not Found\n"
            "[AppID 294100] Download item 12345 result : Failure\n"
        )
        kind, detail = steamcmd._parse_workshop_error("12345")
        assert kind == ErrorKind.FAILURE
        assert "File Not Found" in detail

    def test_wrong_appid(self, steamcmd: SteamCMD, tmp_path: Path):
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[AppID 294100] Get details for item 12345 failed : Wrong AppID 241100\n")
        kind, detail = steamcmd._parse_workshop_error("12345")
        # No result line → the item was never processed; not OK.
        assert kind == ErrorKind.UNKNOWN
        assert "Wrong AppID" in detail

    def test_access_denied_result(self, steamcmd: SteamCMD, tmp_path: Path):
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[AppID 294100] Download item 12345 result : Access Denied\n")
        kind, _ = steamcmd._parse_workshop_error("12345")
        assert kind == ErrorKind.ACCESS_DENIED

    def test_last_result_wins(self, steamcmd: SteamCMD, tmp_path: Path):
        """Multiple lines for the same mod — last result wins."""
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "[AppID 294100] Download item 12345 result : Failure\n"
            "[AppID 294100] Download item 12345 result : OK\n"
        )
        kind, _ = steamcmd._parse_workshop_error("12345")
        assert kind == ErrorKind.OK

    def test_different_mod_not_matched(self, steamcmd: SteamCMD, tmp_path: Path):
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("[AppID 294100] Download item 99999 result : OK\n")
        kind, detail = steamcmd._parse_workshop_error("12345")
        # No result line for *this* mod → the run never processed it.
        assert kind == ErrorKind.UNKNOWN
        assert "没有记录" in detail


class TestEnvironmentGuard:
    """SteamCMD refuses to run from a non-ASCII path; report it clearly."""

    def test_has_non_ascii_path(self):
        assert not SteamCMD._has_non_ascii_path(Path("D:/steamcmd"))
        assert not SteamCMD._has_non_ascii_path(Path("D:/steamcmd/steamcmd.exe"))
        assert SteamCMD._has_non_ascii_path(Path("D:/一/steamcmd"))

    def test_fatal_output_detected(self, steamcmd: SteamCMD):
        detail = steamcmd._fatal_environment_error(
            [
                "!!! Fatal Error",
                "main.cpp (1029) : Unable to access Steam files due to incompatible path",
            ]
        )
        assert detail is not None
        assert "非 ASCII" in detail

    def test_fatal_chinese_output_detected(self, steamcmd: SteamCMD):
        detail = steamcmd._fatal_environment_error(
            [
                "!!! Fatal Error: Steamcmd 在该 Windows 版本上无法从含有非 ASCII 字符的文件夹路径运行。"
            ]
        )
        assert detail is not None

    def test_normal_output_not_flagged(self, steamcmd: SteamCMD):
        assert steamcmd._fatal_environment_error(["Success. Downloaded item 123"]) is None

    def test_non_ascii_dir_rejected_before_spawn(self, tmp_path: Path):
        import os

        if os.name != "nt":
            pytest.skip("SteamCMD path restriction is Windows-only")
        exe = tmp_path / "中文" / "steamcmd.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        sc = SteamCMD(exe)
        from unittest.mock import patch

        with patch("rwmod.steamcmd.subprocess.Popen") as popen:
            results = sc.workshop_download_many(["123"])
        popen.assert_not_called()
        assert results["123"].error_kind == ErrorKind.ENVIRONMENT
        assert "非 ASCII" in results["123"].error_detail


class TestProgressReaderCompleted:
    """P1-1: the reader records which items SteamCMD already finished."""

    def test_completed_items_are_recorded(self):
        import io

        from rwmod.steamcmd import _ProgressReader

        out = io.StringIO(
            "Downloading item 111 ...\nSuccess. Downloaded item 111\nDownloading item 222 ...\n"
        )
        reader = _ProgressReader(out, {"111", "222"}, None)
        reader.start()
        reader.join(2)
        assert reader.completed == {"111"}

    def test_success_for_id_outside_batch_is_ignored(self):
        import io

        from rwmod.steamcmd import _ProgressReader

        reader = _ProgressReader(io.StringIO("Success. Downloaded item 999\n"), {"111"}, None)
        reader.start()
        reader.join(2)
        assert reader.completed == set()


class TestCancelBatch:
    """P1-1: cancelling one id must not lose finished siblings."""

    def _blocking_proc(self):
        import io
        import threading

        class _Proc:
            pid = 1

            def __init__(self) -> None:
                self.stdout = io.StringIO("")
                self._done = threading.Event()

            def wait(self, timeout=None):
                self._done.wait(timeout)
                return 0

            def kill(self) -> None:
                self._done.set()

            def send_signal(self, sig) -> None:
                self._done.set()

        return _Proc()

    def test_cancel_marks_unfinished_ids_and_keeps_completed_sibling(
        self, steamcmd: SteamCMD, monkeypatch
    ):
        import threading
        import time

        from rwmod import steamcmd as sc_mod
        from rwmod.steamcmd import cancel_download

        class _FakeReader:
            def __init__(self, stdout, ids, progress_cb) -> None:
                self.lines: list[str] = []
                self.completed = {"222"}

            def start(self) -> None:
                pass

            def join(self, timeout=None) -> None:
                pass

        proc = self._blocking_proc()
        monkeypatch.setattr("rwmod.steamcmd.subprocess.Popen", lambda *a, **k: proc)
        monkeypatch.setattr("rwmod.steamcmd._ProgressReader", _FakeReader)
        sc_mod._live_procs.clear()
        sc_mod._cancelled_procs.clear()
        results: dict = {}
        thread = threading.Thread(
            target=lambda: results.update(steamcmd.workshop_download_many(["111", "222"]))
        )
        thread.start()
        for _ in range(200):
            with sc_mod._live_guard:
                if "111" in sc_mod._live_procs:
                    break
            time.sleep(0.005)
        assert cancel_download("111") is True
        thread.join(5)
        assert not thread.is_alive()

        assert results["111"].error_kind == ErrorKind.CANCELLED
        assert results["111"].success is False
        assert results["222"].success is True
        assert results["222"].completed is True
        assert "222" not in sc_mod._live_procs

    def test_lock_is_released_after_a_cancelled_batch(self, steamcmd: SteamCMD, monkeypatch):
        import io
        import threading
        import time
        from unittest.mock import MagicMock

        from rwmod import steamcmd as sc_mod
        from rwmod.steamcmd import cancel_download

        class _FakeReader:
            def __init__(self, stdout, ids, progress_cb) -> None:
                self.lines: list[str] = []
                self.completed: set[str] = set()

            def start(self) -> None:
                pass

            def join(self, timeout=None) -> None:
                pass

        first = self._blocking_proc()
        second = MagicMock(wait=MagicMock(return_value=0), stdout=io.StringIO(""), pid=2)
        procs = [first, second]
        monkeypatch.setattr("rwmod.steamcmd.subprocess.Popen", lambda *a, **k: procs.pop(0))
        monkeypatch.setattr("rwmod.steamcmd._ProgressReader", _FakeReader)
        sc_mod._live_procs.clear()
        sc_mod._cancelled_procs.clear()

        thread = threading.Thread(target=lambda: steamcmd.workshop_download_many(["111"]))
        thread.start()
        for _ in range(200):
            with sc_mod._live_guard:
                if "111" in sc_mod._live_procs:
                    break
            time.sleep(0.005)
        cancel_download("111")
        thread.join(5)
        assert not thread.is_alive()

        # A later run must acquire the global lock (P1-3 lock released).
        done = threading.Thread(target=lambda: steamcmd.workshop_download_many(["333"]))
        done.start()
        done.join(5)
        assert not done.is_alive()


class TestGlobalRunLock:
    """P1-3: only one SteamCMD process may run at a time per installation."""

    def test_concurrent_downloads_never_overlap(self, steamcmd: SteamCMD, monkeypatch):
        import io
        import threading
        import time

        state = {"active": 0, "peak": 0, "calls": 0}
        guard = threading.Lock()

        class _SlowProc:
            pid = 1

            def __init__(self) -> None:
                self.stdout = io.StringIO("")

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                with guard:
                    state["active"] += 1
                    state["calls"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                time.sleep(0.15)
                with guard:
                    state["active"] -= 1
                return 0

            def kill(self) -> None:
                pass

        monkeypatch.setattr("rwmod.steamcmd.subprocess.Popen", lambda *args, **kwargs: _SlowProc())
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def run(mid: str) -> None:
            try:
                barrier.wait(timeout=5)
                steamcmd.workshop_download_many([mid])
            except Exception as e:  # noqa: BLE001 — surfaced in the assertion below
                errors.append(e)

        t1 = threading.Thread(target=run, args=("111",))
        t2 = threading.Thread(target=run, args=("222",))
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)

        assert errors == []
        assert not t1.is_alive() and not t2.is_alive()
        assert state["calls"] == 2
        assert state["peak"] == 1  # never two Popen'd processes alive together


class TestSteamCMDProperties:
    def test_workshop_log_path(self, steamcmd: SteamCMD):
        assert steamcmd.workshop_log.name == "workshop_log.txt"
        assert "logs" in str(steamcmd.workshop_log)

    def test_workshop_content_dir(self, steamcmd: SteamCMD):
        assert "294100" in str(steamcmd.workshop_content_dir)
        assert "workshop" in str(steamcmd.workshop_content_dir)


class TestWorkshopDownload:
    """Exercises the real subprocess orchestration with a mocked Popen."""

    def test_timeout(self, steamcmd: SteamCMD):
        from io import StringIO
        from unittest.mock import MagicMock, patch

        proc = MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["steamcmd"], timeout=1)
        proc.stdout = StringIO("")
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc):
            result = steamcmd.workshop_download("123")
        assert result.error_kind == ErrorKind.TIMEOUT
        proc.kill.assert_called_once()

    def test_success(self, steamcmd: SteamCMD):
        from io import StringIO
        from unittest.mock import MagicMock, patch

        content = steamcmd.workshop_content_dir / "123"
        content.mkdir(parents=True)
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("[AppID 294100] Download item 123 result : OK\n")

        proc = MagicMock()
        proc.wait.return_value = 0
        proc.stdout = StringIO("[AppID 294100] Download item 123 result : OK\n")
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc):
            result = steamcmd.workshop_download("123")
        assert result.success
        assert result.output_lines[0].endswith("result : OK")

    def test_missing_content_treated_as_failure(self, steamcmd: SteamCMD):
        from io import StringIO
        from unittest.mock import MagicMock, patch

        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("[AppID 294100] Download item 123 result : OK\n")

        proc = MagicMock()
        proc.wait.return_value = 0
        proc.stdout = StringIO("[AppID 294100] Download item 123 result : OK\n")
        # offset=0 → the pre-written log line counts as this run's output.
        with (
            patch("rwmod.steamcmd.subprocess.Popen", return_value=proc),
            patch.object(SteamCMD, "_log_offset", return_value=0),
        ):
            result = steamcmd.workshop_download("123")
        assert not result.success
        assert result.error_kind == ErrorKind.FAILURE
        assert "找不到" in result.error_detail

    def test_oserror_startup(self, steamcmd: SteamCMD):
        from unittest.mock import patch

        with patch("rwmod.steamcmd.subprocess.Popen", side_effect=OSError("no exe")):
            result = steamcmd.workshop_download("123")
        assert not result.success
        assert result.error_kind == ErrorKind.UNKNOWN

    def test_progress_callback_fires(self, steamcmd: SteamCMD):
        """The reader thread parses SteamCMD progress lines into callbacks."""
        from io import StringIO
        from unittest.mock import MagicMock, patch

        content = steamcmd.workshop_content_dir / "123"
        content.mkdir(parents=True)
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("[AppID 294100] Download item 123 result : OK\n")

        out = (
            "Downloading item 123 ...\n"
            "Update state (0x61) downloading, progress: 12.50 (100 / 800)\n"
            "Update state (0x61) downloading, progress: 50.00 (400 / 800)\n"
            "Update state (0x61) downloading, progress: 100.00 (800 / 800)\n"
            "Success. Downloaded item 123\n"
        )
        proc = MagicMock()
        proc.wait.return_value = 0
        proc.stdout = StringIO(out)
        seen: list[tuple[str, float, float, float]] = []
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc):
            steamcmd.workshop_download_many(["123"], progress_cb=lambda *a: seen.append(a))
        assert len(seen) == 3
        assert seen[0] == ("123", 12.5, 100.0, 800.0)
        assert seen[2] == ("123", 100.0, 800.0, 800.0)


class TestCancelDownload:
    """cancel_download() must kill a registered in-flight SteamCMD process."""

    def test_cancel_registered_process(self, steamcmd: SteamCMD):
        from unittest.mock import MagicMock

        from rwmod.steamcmd import _live_procs, cancel_download

        proc = MagicMock()
        _live_procs.clear()
        _live_procs["12345"] = proc
        try:
            assert cancel_download("12345") is True
            # CTRL_BREAK (or terminate on POSIX) then kill as belt-and-braces.
            assert proc.kill.called
        finally:
            _live_procs.clear()

    def test_cancel_unknown_mod_returns_false(self):
        from rwmod.steamcmd import _live_procs, cancel_download

        _live_procs.clear()
        try:
            assert cancel_download("99999") is False
        finally:
            _live_procs.clear()

    def test_workshop_download_unregisters_after_finish(self, steamcmd: SteamCMD):
        """The registry must not leak: after a download finishes the slot is freed."""
        from io import StringIO
        from unittest.mock import MagicMock, patch

        from rwmod.steamcmd import _live_procs

        _live_procs.clear()
        content = steamcmd.workshop_content_dir / "123"
        content.mkdir(parents=True)
        log = steamcmd.steam_dir / "logs" / "workshop_log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("[AppID 294100] Download item 123 result : OK\n")

        proc = MagicMock()
        proc.wait.return_value = 0
        proc.stdout = StringIO("[AppID 294100] Download item 123 result : OK\n")
        with patch("rwmod.steamcmd.subprocess.Popen", return_value=proc):
            result = steamcmd.workshop_download("123")
        assert result.success
        assert "123" not in _live_procs
