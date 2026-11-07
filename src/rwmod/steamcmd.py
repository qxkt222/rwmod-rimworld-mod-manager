"""Thin subprocess wrapper around SteamCMD — with structured error reporting.

The implementation is split into three layers under
:mod:`rwmod.steamcmd_support` while this module keeps the historical
``rwmod.steamcmd`` import surface intact:

* :mod:`rwmod.steamcmd_support.taxonomy` — ErrorKind + DownloadResult + verdict;
* :mod:`rwmod.steamcmd_support.logparse` — workshop_log.txt parsing;
* :mod:`rwmod.steamcmd_support.procs` — live-process registry, cancellation,
  progress.

:class:`SteamCMD` stays here so that ``rwmod.steamcmd.subprocess.Popen`` and
``rwmod.steamcmd._ProgressReader`` remain patchable at their historical paths.
(The support package cannot be named ``rwmod.steamcmd``: the repo's
``.gitignore`` ignores every ``steamcmd/`` directory.)
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess  # nosec B404 — subprocess is required to run SteamCMD
from pathlib import Path

from rwmod.steamcmd_support.logparse import classify_reason, parse_workshop_error
from rwmod.steamcmd_support.procs import (
    _LOCK_POLL_SECS,
    ProgressCallback,
    _cancelled_procs,
    _consume_cancel_requests,
    _forget_cancel_requests,
    _has_cancel_request,
    _live_guard,
    _live_procs,
    _proc_key,
    _ProgressReader,
    _run_lock,
    active_download_count,
    cancel_download,
    request_cancel,
)
from rwmod.steamcmd_support.taxonomy import DownloadResult, ErrorKind, _decide_download_result

__all__ = [
    "SteamCMD",
    "DownloadResult",
    "ErrorKind",
    "cancel_download",
    "request_cancel",
    "active_download_count",
]

_log = logging.getLogger(__name__)


def _cancelled_results(mod_ids: list[str]) -> dict[str, DownloadResult]:
    """Every id of a batch that was cancelled before SteamCMD even spawned."""
    return {
        mid: DownloadResult(
            success=False,
            mod_id=mid,
            error_kind=ErrorKind.CANCELLED,
            error_detail="SteamCMD 批次被用户取消",
        )
        for mid in mod_ids
    }


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

    # Output markers SteamCMD prints when it cannot run from its own path.
    # The message is localized (Chinese on a zh-CN Windows install), so match
    # both the English and the Chinese form.
    _ENV_OUTPUT_MARKERS = ("incompatible path", "非 ascii", "非ascii")

    @staticmethod
    def _has_non_ascii_path(path: Path) -> bool:
        return any(ord(ch) > 127 for ch in str(path))

    def environment_error(self) -> str | None:
        """Return a human-readable detail when SteamCMD cannot run here.

        SteamCMD on Windows refuses to start when its own directory contains
        non-ASCII characters — it exits within a second, writes no
        workshop_log lines, and the old code then reported the misleading
        "SteamCMD 返回成功但未找到下载内容" for every item. Detecting it up
        front turns that into one actionable message.
        """
        if os.name == "nt" and self._has_non_ascii_path(self.steam_dir):
            return (
                f"SteamCMD 无法从含非 ASCII 字符的路径运行：{self.steam_dir}。"
                "请把 SteamCMD 移到纯英文路径（例如 D:\\steamcmd），"
                "并在设置中更新 steamcmd 路径。"
            )
        return None

    def _fatal_environment_error(self, lines: list[str]) -> str | None:
        """Detect the same failure from a run's output (path escaped the
        pre-check, e.g. a non-ASCII cwd symlink)."""
        joined = "\n".join(lines).lower()
        if any(marker in joined for marker in self._ENV_OUTPUT_MARKERS):
            return (
                f"SteamCMD 无法从路径 {self.steam_dir} 运行（路径含非 ASCII 字符）。"
                "请把 SteamCMD 移到纯英文路径，并在设置中更新 steamcmd 路径。"
            )
        return None

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

    def workshop_download(self, mod_id: str, instance_id: str | None = None) -> DownloadResult:
        """Download a single workshop item (batch of one)."""
        return self.workshop_download_many([mod_id], instance_id=instance_id)[mod_id]

    def workshop_download_many(
        self,
        mod_ids: list[str],
        progress_cb: ProgressCallback | None = None,
        instance_id: str | None = None,
    ) -> dict[str, DownloadResult]:
        """Download several workshop items in ONE SteamCMD process.

        SteamCMD accepts repeated ``+workshop_download_item`` args in a single
        invocation — one ``+login anonymous``, one process, N downloads. This
        removes the per-mod process startup/login overhead (several seconds
        each) and lets SteamCMD run the downloads back-to-back without the
        login handshake in between.

        The process is registered in ``_live_procs`` under *every*
        ``(instance_id, mod id)`` pair in the batch, so queue cancellation of
        any one of them kills the whole batch (the sibling items then fail
        fast and are re-queued by the caller). The instance scopes the
        registry: the same workshop id may be downloading for another
        installation at the same time, and only this instance's copy may be
        cancelled.

        ``progress_cb(mod_id, percent, downloaded, total)`` is invoked from a
        reader thread as SteamCMD reports byte progress (only for ids in the
        batch; the callback must be cheap and thread-safe).

        Cancellation is honoured during the probe phase and while waiting for
        the global run lock: a pending ``request_cancel()`` for any batch item
        aborts the batch before a process is spawned (all ids report
        CANCELLED, exactly like a batch killed mid-flight).

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

        # A non-ASCII SteamCMD path makes SteamCMD exit before it downloads
        # anything. Reject up front with the real reason instead of letting
        # every item fail later as "success but no content".
        env_error = self.environment_error()
        if env_error is not None:
            return {
                mid: DownloadResult(
                    success=False,
                    mod_id=mid,
                    error_kind=ErrorKind.ENVIRONMENT,
                    error_detail=env_error,
                )
                for mid in valid
            }

        # Cancellation may have arrived while the caller was probing (before
        # any process existed). Consume it here instead of spawning anyway.
        if _consume_cancel_requests(instance_id, valid):
            return _cancelled_results(valid)

        cmd = [self.exe, "+login", "anonymous"]
        for mid in valid:
            cmd += ["+workshop_download_item", self.STEAM_APP_ID, mid]
        cmd += ["+quit"]

        # P1-3: serialize every SteamCMD run per installation. Queue batches,
        # SSE/direct downloads and the mods router all funnel through here;
        # concurrent processes would share logs/, appcache/, depotcache/ and
        # the anonymous login state. Progress callbacks and cancel_download()
        # keep working — the lock covers only the child process lifetime.
        reader: _ProgressReader | None = None
        batch_cancelled = False
        locked = False
        try:
            # Wait for the global lock in short slices so a cancel request is
            # seen even while another batch owns SteamCMD for minutes.
            while not _run_lock.acquire(timeout=_LOCK_POLL_SECS):
                if _has_cancel_request(instance_id, valid):
                    _consume_cancel_requests(instance_id, valid)
                    return _cancelled_results(valid)
            locked = True

            # The cancel may have landed between the poll and acquisition.
            if _consume_cancel_requests(instance_id, valid):
                return _cancelled_results(valid)

            log_offset = self._log_offset()

            # Total timeout scales with batch size (each item gets the 10-min
            # budget of the single-item path) but is capped so a giant batch
            # can never pin a worker for hours.
            timeout_secs = min(
                40 * 60,
                max(self._TIMEOUT_MINUTES * 60, 5 * 60 * len(valid)),
            )

            # Windows: a new process group is what makes cancel_download()'s
            # CTRL_BREAK_EVENT reach *this* child (and its download children)
            # instead of the server's own console group — without the flag the
            # signal is silently not delivered and can even terminate the
            # caller's shell. Non-Windows must pass 0 (Windows-only flag).
            creationflags = 0
            if os.name == "nt":
                creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

            # Last check before spawning: another cancel may have arrived.
            if _consume_cancel_requests(instance_id, valid):
                return _cancelled_results(valid)

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
                    creationflags=creationflags,
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

            try:
                # Register under every (instance, id) pair so
                # cancel_download(instance_id, mod_id) can kill the shared
                # process from any of the batch's items.
                with _live_guard:
                    for mid in valid:
                        _live_procs[_proc_key(instance_id, mid)] = proc
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
                # The process has exited (or was killed) — free all slots.
                with _live_guard:
                    batch_cancelled = proc in _cancelled_procs
                    _cancelled_procs.discard(proc)
                    for mid in valid:
                        _live_procs.pop(_proc_key(instance_id, mid), None)
                if reader is not None:
                    reader.join(timeout=5)
                with contextlib.suppress(Exception):
                    if proc.stdout:
                        proc.stdout.close()
        finally:
            if locked:
                _run_lock.release()
            # No stale pending marker may leak into a later batch of the same
            # (instance, id) after this one is over.
            _forget_cancel_requests(instance_id, valid)

        if batch_cancelled:
            # The user killed the shared process. Report CANCELLED for every
            # item SteamCMD had not finished; items that printed "Success"
            # are already on disk and are reported as successes so the caller
            # installs them instead of re-downloading the whole batch.
            cancelled_lines = list(reader.lines) if reader is not None else []
            completed = set(reader.completed) if reader is not None else set()
            return {
                mid: DownloadResult(
                    success=mid in completed,
                    mod_id=mid,
                    error_kind=ErrorKind.OK if mid in completed else ErrorKind.CANCELLED,
                    error_detail="" if mid in completed else "SteamCMD 批次被用户取消",
                    output_lines=cancelled_lines,
                    completed=mid in completed,
                )
                for mid in valid
            }

        lines: list[str] = list(reader.lines) if reader is not None else []

        # SteamCMD may still refuse to start for path reasons the pre-check
        # could not see (e.g. a non-ASCII cwd reached through a link).
        fatal_env = self._fatal_environment_error(lines)
        if fatal_env is not None:
            return {
                mid: DownloadResult(
                    success=False,
                    mod_id=mid,
                    error_kind=ErrorKind.ENVIRONMENT,
                    error_detail=fatal_env,
                    output_lines=lines,
                )
                for mid in valid
            }

        # ── parse workshop_log.txt (only this run's appended section) ──
        for mid in valid:
            content_exists = (self.workshop_content_dir / mid).exists()
            error_kind, error_detail = self._parse_workshop_error(mid, log_offset)
            results[mid] = _decide_download_result(
                mid, error_kind, error_detail, content_exists, lines
            )
        return results

    def _parse_workshop_error(self, mod_id: str, offset: int = 0) -> tuple[str, str]:
        """Parse workshop_log.txt to extract the real error reason.

        Thin wrapper around :func:`rwmod.steamcmd.logparse.parse_workshop_error`
        (kept as a method for callers/tests written against the old class).
        """
        return parse_workshop_error(self.workshop_log, self.STEAM_APP_ID, mod_id, offset)

    @staticmethod
    def _classify_reason(reason: str) -> str:
        """Map SteamCMD result strings to our ErrorKind taxonomy."""
        return classify_reason(reason)
