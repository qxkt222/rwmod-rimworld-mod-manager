"""Thin subprocess wrapper around SteamCMD — with structured error reporting."""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess  # nosec B404 — subprocess is required to run SteamCMD
import threading
from dataclasses import dataclass, field
from pathlib import Path

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
            proc.send_signal(signal.CTRL_BREAK_EVENT)
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
        """Download a workshop item, return structured result with error classification."""
        # Last-line defense: workshop IDs must be purely numeric so a crafted
        # value can never inject SteamCMD command tokens (e.g.
        # "+force_install_dir <path>" or "+download_depot ...").
        if not mod_id.isdigit():
            return DownloadResult(
                success=False,
                mod_id=mod_id,
                error_kind=ErrorKind.UNKNOWN,
                error_detail=f"非法的 Mod ID: {mod_id!r}",
            )
        cmd = [
            self.exe,
            "+login",
            "anonymous",
            "+workshop_download_item",
            self.STEAM_APP_ID,
            mod_id,
            "+quit",
        ]
        log_offset = self._log_offset()

        try:
            proc = subprocess.Popen(  # nosec B603 — command list is fixed, no shell, no user input
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=-1,
                cwd=str(self.steam_dir),
            )
        except OSError as e:
            return DownloadResult(
                success=False,
                mod_id=mod_id,
                error_kind=ErrorKind.UNKNOWN,
                error_detail=f"无法启动 SteamCMD: {e}",
            )

        # Register so queue.remove() / cancel_download() can kill this run.
        with _live_guard:
            _live_procs[mod_id] = proc
        try:
            out, _err = proc.communicate(timeout=self._TIMEOUT_MINUTES * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # TerminateProcess is asynchronous on Windows / AV can delay
                # it — the process may still be alive; log it so it can't
                # silently linger and hold workshop locks.
                _log.warning("SteamCMD %s 超时且 kill 后 30s 仍未退出（进程可能残留）", mod_id)
            return DownloadResult(
                success=False,
                mod_id=mod_id,
                error_kind=ErrorKind.TIMEOUT,
                error_detail="SteamCMD 超时无响应",
            )
        finally:
            # The process has exited (or was killed) — free the registry slot.
            with _live_guard:
                _live_procs.pop(mod_id, None)

        lines: list[str] = [line for line in (out or "").splitlines() if line.strip()]

        # ── parse workshop_log.txt (only this run's appended section) ──
        error_kind, error_detail = self._parse_workshop_error(mod_id, log_offset)

        if error_kind == ErrorKind.OK:
            # Double-check the content actually exists
            content_dir = self.workshop_content_dir / mod_id
            if content_dir.exists():
                return DownloadResult(
                    success=True,
                    mod_id=mod_id,
                    error_kind=ErrorKind.OK,
                    output_lines=lines,
                )
            # SteamCMD said OK but file not on disk → treat as failure
            error_kind = ErrorKind.FAILURE
            error_detail = "SteamCMD 返回成功但未找到下载内容"

        return DownloadResult(
            success=False,
            mod_id=mod_id,
            error_kind=error_kind,
            error_detail=error_detail,
            output_lines=lines,
        )

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
