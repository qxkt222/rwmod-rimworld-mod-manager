"""Thin subprocess wrapper around SteamCMD — with structured error reporting."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import subprocess  # nosec B404 — subprocess is required to run SteamCMD
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

__all__ = [
    "SteamCMD",
    "DownloadResult",
    "ErrorKind",
    "cancel_download",
    "active_download_count",
]

_log = logging.getLogger(__name__)

# ── live-process registry ──────────────────────────────────────────
# Queue.remove() can cancel an in-flight download, but SteamCMD keeps running
# to completion (up to 10 min) unless we actually kill it. We track every
# live SteamCMD subprocess here so cancel_download() can terminate it (and on
# Windows send CTRL_BREAK so child processes — SteamCMD's own downloads — get
# the message too, instead of orphaned steam processes lingering).
_live_procs: dict[str, subprocess.Popen] = {}
_live_guard = threading.Lock()


def cancel_download(mod_id: str) -> bool:
    """Kill the in-flight SteamCMD subprocess for ``mod_id``, if any.

    Returns True if a process was found and terminated.
    """
    with _live_guard:
        proc = _live_procs.get(mod_id)
        if proc is None:
            return False
    _log.info("终止 SteamCMD 进程（mod %s, pid %s）", mod_id, proc.pid)
    try:
        if os.name == "nt":
            # CTRL_BREAK_EVENT reaches the whole process group on Windows;
            # proc.kill() alone would orphan SteamCMD's download children.
            # It is only defined by typeshed under sys.platform == "win32",
            # so access it dynamically to keep mypy green on POSIX too.
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                proc.send_signal(ctrl_break)
            else:
                proc.terminate()
        else:
            proc.terminate()
        proc.kill()  # belt and braces — CTRL_BREAK is advisory on some setups
    except (OSError, subprocess.SubprocessError):
        with _live_guard:
            _live_procs.pop(mod_id, None)
        return False
    with _live_guard:
        _live_procs.pop(mod_id, None)
    return True


def active_download_count() -> int:
    """Number of SteamCMD subprocesses currently running."""
    with _live_guard:
        return len(_live_procs)


# ── error taxonomy ──────────────────────────────────────────────────
# SteamCMD workshop_log.txt records the real reason for every failure.
# We parse it so the downloader can skip pointless retries.


class ErrorKind:
    """SteamCMD error categories from workshop_log.txt."""

    OK = "ok"
    FAILURE = "failure"  # depot/manifest error — mod exists but can't download
    FILE_NOT_FOUND = "file_not_found"  # mod removed from workshop
    ACCESS_DENIED = "access_denied"  # private/hidden mod
    NO_MATCH = "no_match"  # wrong game AppID
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"

    # Errors that will NEVER succeed on retry → skip retry + Skymods
    NON_RETRYABLE = {FAILURE, FILE_NOT_FOUND, ACCESS_DENIED, NO_MATCH}

    _HUMAN: dict[str, str] = {
        OK: "下载成功",
        FAILURE: "Mod 存在但文件不可用（可能已被作者删除或设为私密）",
        FILE_NOT_FOUND: "Mod 不存在（已从创意工坊移除）",
        ACCESS_DENIED: "Mod 为私密/隐藏状态，无法匿名下载",
        NO_MATCH: "不是 RimWorld 的 Mod（属于其他游戏）",
        TIMEOUT: "SteamCMD 超时无响应",
        UNKNOWN: "未知错误",
    }

    @classmethod
    def explain(cls, kind: str) -> str:
        return cls._HUMAN.get(kind, f"未知错误类型: {kind}")


@dataclass
class DownloadResult:
    """Structured download result with error classification."""

    success: bool
    mod_id: str
    error_kind: str = ErrorKind.UNKNOWN
    error_detail: str = ""
    output_lines: list[str] = field(default_factory=list)


# Progress callback signature: (mod_id, percent, downloaded_bytes, total_bytes).
ProgressCallback = Callable[[str, float, float, float], None]


class _ProgressReader(threading.Thread):
    """Reads SteamCMD stdout line-by-line and reports live download progress.

    SteamCMD prints lines like::

        Downloading item 2822234567 ...
        Update state (0x61) downloading, progress: 42.30 (360448 / 2912345)
        ...
        Success. Downloaded item 2822234567

    The ``Downloading item`` line tells us *which* mod the following progress
    lines belong to; the ``Update state ... downloading`` lines carry the
    real byte counts. All lines are kept for the caller's output_lines.
    """

    _PROGRESS_RE = re.compile(
        r"Update state \(0x[0-9a-fA-F]+\) downloading, progress: ([\d.]+) \((\d+) / (\d+)\)"
    )
    _ITEM_START_RE = re.compile(r"Downloading item (\d+)")
    _ITEM_END_RE = re.compile(r"Success\. Downloaded item (\d+)|Download item (\d+) result")

    def __init__(
        self,
        stdout: IO[str] | None,
        mod_ids: set[str],
        progress_cb: ProgressCallback | None,
    ) -> None:
        super().__init__(daemon=True, name="rwmod-steamcmd-reader")
        self._fh = stdout
        self._mod_ids = mod_ids
        self._cb = progress_cb
        self.lines: list[str] = []
        self.current_id: str | None = None

    def run(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(Exception):  # broken pipe / closed stdout on kill
            for raw in self._fh:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                self.lines.append(line)
                with contextlib.suppress(Exception):  # a parse hiccup must not kill the reader
                    self._parse(line)

    def _parse(self, line: str) -> None:
        m = self._ITEM_START_RE.search(line)
        if m and m.group(1) in self._mod_ids:
            self.current_id = m.group(1)
            return
        if self._ITEM_END_RE.search(line):
            self.current_id = None
            return
        if self._cb is None or self.current_id is None:
            return
        m = self._PROGRESS_RE.search(line)
        if m and self._cb is not None:
            with contextlib.suppress(Exception):  # callback must never break the reader
                self._cb(
                    self.current_id,
                    float(m.group(1)),
                    float(m.group(2)),
                    float(m.group(3)),
                )


class SteamCMD:
    """Run SteamCMD commands with structured error parsing."""

    STEAM_APP_ID = "294100"
    _TIMEOUT_MINUTES = 10  # max wait for SteamCMD to finish

    def __init__(self, steamcmd_path: Path) -> None:
        self.exe = str(steamcmd_path)
        self.steam_dir = steamcmd_path.parent

    @property
    def workshop_log(self) -> Path:
        """Path to workshop_log.txt where SteamCMD writes error details."""
        return self.steam_dir / "logs" / "workshop_log.txt"

    @property
    def workshop_content_dir(self) -> Path:
        """Directory where SteamCMD stores downloaded workshop content."""
        return self.steam_dir / "steamapps" / "workshop" / "content" / self.STEAM_APP_ID

    def _log_offset(self) -> int:
        """Snapshot of workshop_log.txt size *before* this run.

        SteamCMD appends every run to the shared workshop_log.txt. With
        concurrent runs (we allow up to MAX_CONCURRENT_DOWNLOADS processes)
        another process's lines could be mis-attributed to this mod. By
        recording the size before launch we can parse only the bytes this
        process appended, keeping error classification race-free.
        """
        if not self.workshop_log.exists():
            return 0
        try:
            return self.workshop_log.stat().st_size
        except OSError:
            return 0

    def workshop_download(self, mod_id: str) -> DownloadResult:
        """Download a single workshop item (batch of one)."""
        return self.workshop_download_many([mod_id])[mod_id]

    def workshop_download_many(
        self,
        mod_ids: list[str],
        progress_cb: ProgressCallback | None = None,
    ) -> dict[str, DownloadResult]:
        """Download several workshop items in ONE SteamCMD process.

        SteamCMD accepts repeated ``+workshop_download_item`` args in a single
        invocation — one ``+login anonymous``, one process, N downloads. This
        removes the per-mod process startup/login overhead (several seconds
        each) and lets SteamCMD run the downloads back-to-back without the
        login handshake in between.

        The process is registered in ``_live_procs`` under *every* mod id in
        the batch, so queue cancellation of any one of them kills the whole
        batch (the sibling items then fail fast and are re-queued by the
        caller).

        ``progress_cb(mod_id, percent, downloaded, total)`` is invoked from a
        reader thread as SteamCMD reports byte progress (only for ids in the
        batch; the callback must be cheap and thread-safe).

        Returns a dict keyed by mod id. Invalid (non-numeric) ids are
        rejected up front without launching SteamCMD.
        """
        results: dict[str, DownloadResult] = {}
        valid: list[str] = []
        for mid in mod_ids:
            # Last-line defense: workshop IDs must be purely numeric so a
            # crafted value can never inject SteamCMD command tokens (e.g.
            # "+force_install_dir <path>" or "+download_depot ...").
            if mid.isdigit():
                valid.append(mid)
            else:
                results[mid] = DownloadResult(
                    success=False,
                    mod_id=mid,
                    error_kind=ErrorKind.UNKNOWN,
                    error_detail=f"非法的 Mod ID: {mid!r}",
                )
        if not valid:
            return results

        cmd = [self.exe, "+login", "anonymous"]
        for mid in valid:
            cmd += ["+workshop_download_item", self.STEAM_APP_ID, mid]
        cmd += ["+quit"]
        log_offset = self._log_offset()

        # Total timeout scales with batch size (each item gets the 10-min
        # budget of the single-item path) but is capped so a giant batch can
        # never pin a worker for hours.
        timeout_secs = min(
            40 * 60,
            max(self._TIMEOUT_MINUTES * 60, 5 * 60 * len(valid)),
        )

        try:
            proc = subprocess.Popen(  # nosec B603 — command list is fixed, no shell, no user input
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line-buffered — progress lines arrive promptly
                cwd=str(self.steam_dir),
            )
        except OSError as e:
            return {
                mid: DownloadResult(
                    success=False,
                    mod_id=mid,
                    error_kind=ErrorKind.UNKNOWN,
                    error_detail=f"无法启动 SteamCMD: {e}",
                )
                for mid in valid
            }

        # Register under every id so cancel_download(mod_id) can kill the
        # shared process from any of the batch's items.
        with _live_guard:
            for mid in valid:
                _live_procs[mid] = proc
        reader: _ProgressReader | None = None
        try:
            # A reader thread drains stdout line-by-line while the process
            # runs, so progress callbacks fire in real time (communicate()
            # would buffer everything until exit).
            reader = _ProgressReader(proc.stdout, set(valid), progress_cb)
            reader.start()
            proc.wait(timeout=timeout_secs)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _log.warning(
                    "SteamCMD 批量 %s 超时且 kill 后 30s 仍未退出（进程可能残留）",
                    valid[:5],
                )
            if reader is not None:
                reader.join(timeout=5)
            with contextlib.suppress(Exception):
                if proc.stdout:
                    proc.stdout.close()
            return {
                mid: DownloadResult(
                    success=False,
                    mod_id=mid,
                    error_kind=ErrorKind.TIMEOUT,
                    error_detail="SteamCMD 批量下载超时",
                )
                for mid in valid
            }
        finally:
            # The process has exited (or was killed) — free all registry slots.
            with _live_guard:
                for mid in valid:
                    _live_procs.pop(mid, None)
            if reader is not None:
                reader.join(timeout=5)
            with contextlib.suppress(Exception):
                if proc.stdout:
                    proc.stdout.close()

        lines: list[str] = list(reader.lines) if reader is not None else []

        # ── parse workshop_log.txt (only this run's appended section) ──
        for mid in valid:
            error_kind, error_detail = self._parse_workshop_error(mid, log_offset)
            if error_kind == ErrorKind.OK:
                # Double-check the content actually exists
                content_dir = self.workshop_content_dir / mid
                if content_dir.exists():
                    results[mid] = DownloadResult(
                        success=True,
                        mod_id=mid,
                        error_kind=ErrorKind.OK,
                        output_lines=lines,
                    )
                    continue
                # SteamCMD said OK but file not on disk → treat as failure
                error_kind = ErrorKind.FAILURE
                error_detail = "SteamCMD 返回成功但未找到下载内容"
            results[mid] = DownloadResult(
                success=False,
                mod_id=mid,
                error_kind=error_kind,
                error_detail=error_detail,
                output_lines=lines,
            )
        return results

    def _parse_workshop_error(self, mod_id: str, offset: int = 0) -> tuple[str, str]:
        """Parse workshop_log.txt to extract the real error reason.

        Workshop log records look like:
          [AppID 294100] Download item 3565275325 result : Failure
          [AppID 294100] Get details for item 3565275325 failed : File Not Found
          [AppID 294100] Download item 3565275325 result : OK

        Args:
            mod_id: The workshop item ID to look for.
            offset: Bytes offset into the log; only the appended section
                    (this process's run) is searched. 0 means the whole file.
        """
        log_path = self.workshop_log
        if not log_path.exists():
            return ErrorKind.UNKNOWN, "workshop_log.txt 不存在"

        try:
            if offset:
                # Read only the bytes appended since this run started.
                with log_path.open("rb") as fh:
                    fh.seek(offset)
                    raw = fh.read()
                text = raw.decode("utf-8", errors="replace")
            else:
                text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ErrorKind.UNKNOWN, "无法读取 workshop_log.txt"

        # ── extract the *last* result line for this mod ID ────────
        # Pattern: [AppID 294100] Download item {mod_id} result : {reason}
        result_pattern = (
            rf"\[AppID {self.STEAM_APP_ID}\] Download item {re.escape(mod_id)} result : (.+)"
        )
        result_matches = list(re.finditer(result_pattern, text))

        # Pattern: [AppID 294100] Get details for item {mod_id} failed : {reason}
        detail_pattern = (
            rf"\[AppID {self.STEAM_APP_ID}\] Get details for item {re.escape(mod_id)} failed : (.+)"
        )
        detail_matches = list(re.finditer(detail_pattern, text))

        error_kind = ErrorKind.OK
        error_detail = ""

        # Parse the last result line
        if result_matches:
            raw_reason = result_matches[-1].group(1).strip()
            error_kind = self._classify_reason(raw_reason)
            error_detail = f"SteamCMD: {raw_reason}"

        # Detail lines give more specific info (e.g. "Wrong AppID 241100")
        if detail_matches:
            raw_detail = detail_matches[-1].group(1).strip()
            if raw_detail and raw_detail != error_kind.replace("_", " ").title():
                error_detail = f"SteamCMD: {raw_detail}"

        return error_kind, error_detail

    @staticmethod
    def _classify_reason(reason: str) -> str:
        """Map SteamCMD result strings to our ErrorKind taxonomy."""
        r = reason.strip().lower()
        if r == "ok":
            return ErrorKind.OK
        if "file not found" in r:
            return ErrorKind.FILE_NOT_FOUND
        if "access denied" in r:
            return ErrorKind.ACCESS_DENIED
        if "no match" in r:
            return ErrorKind.NO_MATCH
        if "failure" in r:
            return ErrorKind.FAILURE
        return ErrorKind.UNKNOWN
