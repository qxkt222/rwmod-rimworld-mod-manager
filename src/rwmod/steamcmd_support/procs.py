"""Live SteamCMD process management, cancellation and progress reading.

Queue.remove() can cancel an in-flight download, but SteamCMD keeps running
to completion (up to 10 min) unless we actually kill it. This module owns the
registry of live subprocesses and the cancel primitives. Cancellation can
also arrive *before* a process exists (the batch is still probing or waiting
for the global run lock), so a pending request set is maintained as well.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import subprocess  # nosec B404 — subprocess is required to run SteamCMD
import threading
from collections.abc import Callable
from typing import IO

_log = logging.getLogger(__name__)

# Registry key for every cancellation structure below. Cancellation is scoped
# to one installation: the same workshop id may be downloading for two RimWorld
# installs at the same time (two different mods directories / SteamCMD roots),
# and cancelling alpha's copy must never kill beta's process. Older code keyed
# these tables by workshop id alone, so cancelling either killed both.
ProcessKey = tuple[str, str]


def _proc_key(instance_id: str | None, mod_id: str) -> ProcessKey:
    """Return the ``(instance_id, workshop id)`` key for the registries."""
    return (instance_id or "", mod_id)


# ── live-process registry ──────────────────────────────────────────
# _live_procs maps every (instance, id) of a running batch to the shared Popen
# object so cancel_download() can terminate it (and on Windows send CTRL_BREAK
# so child processes — SteamCMD's own downloads — get the message too, instead
# of orphaned steam processes lingering).
_live_procs: dict[ProcessKey, subprocess.Popen] = {}
_live_guard = threading.Lock()

# Batches that cancel_download() killed (the Popen objects themselves — held
# by identity, not id(), so a garbage-collected mock cannot alias a new
# process). The batch needs this to tell "the process died because the user
# cancelled" from a real failure and report CANCELLED instead of re-downloading
# every item. Guarded by _live_guard; entries are consumed and removed by
# workshop_download_many. The cap only matters for tests that register a mock
# in _live_procs directly and never run a batch.
_cancelled_procs: set[object] = set()
_CANCELLED_PROCS_MAX = 64

# Cancellation requests for (instance, id) whose process does not exist yet:
# the batch may be probing or blocked on the global run lock. queue.remove()
# records the request here (via request_cancel) so the batch aborts before
# spawning instead of downloading a mod the user already cancelled.
_cancel_requests: set[ProcessKey] = set()
_CANCEL_REQUESTS_MAX = 1024

# ── global run lock (P1-3) ─────────────────────────────────────────
# SteamCMD installations are not concurrency-safe: every process shares
# logs/workshop_log.txt, appcache/, depotcache/, steamapps/ and the anonymous
# login state. One module-level lock serializes every run (queue batches,
# SSE/direct downloads, the mods router) so only one child process exists at a
# time. It is deliberately a plain Lock, not a Semaphore: keeping the public
# MAX_CONCURRENT semantics untouched while making the actual execution serial.
_run_lock = threading.Lock()

#: How often a waiting batch re-checks the cancel-request set.
_LOCK_POLL_SECS = 0.1


def request_cancel(instance_id: str | None, mod_id: str) -> bool:
    """Request cancellation of a download, whether or not it has started.

    Cancellation is scoped to *instance_id*: the same workshop id downloading
    for two installations is handled by two independent batches, and only the
    named instance's copy may be aborted.

    Always records a pending request: the batch may still be probing or
    waiting for the global lock, in which case there is no process to kill
    yet and the batch consumes the request before spawning. When a process
    *is* live, it is killed immediately.

    Queue integration contract: ``queue.remove()`` should call this (instead
    of only ``cancel_download``) for an item that is already marked
    downloading, because the batch may not have reached Popen yet. Calling it
    for pending/finished items is wrong — the marker would outlive the job.

    Returns True when a live process was killed (same contract as
    :func:`cancel_download`); a pending-only request returns False.
    """
    key = _proc_key(instance_id, mod_id)
    with _live_guard:
        if len(_cancel_requests) >= _CANCEL_REQUESTS_MAX:
            _cancel_requests.clear()
        _cancel_requests.add(key)
    return cancel_download(instance_id, mod_id)


def _has_cancel_request(instance_id: str | None, mod_ids: list[str]) -> bool:
    with _live_guard:
        return any(_proc_key(instance_id, mid) in _cancel_requests for mid in mod_ids)


def _consume_cancel_requests(instance_id: str | None, mod_ids: list[str]) -> set[str]:
    """Remove and return the cancel requests recorded for these ids."""
    with _live_guard:
        keys = {_proc_key(instance_id, mid) for mid in mod_ids}
        hit = keys & _cancel_requests
        _cancel_requests.difference_update(hit)
    return {mid for _, mid in hit}


def _forget_cancel_requests(instance_id: str | None, mod_ids: list[str]) -> None:
    """Drop requests for ids whose batch already finished (no stale markers)."""
    with _live_guard:
        for mid in mod_ids:
            _cancel_requests.discard(_proc_key(instance_id, mid))


def cancel_download(instance_id: str | None, mod_id: str) -> bool:
    """Kill the in-flight SteamCMD subprocess for ``(instance_id, mod_id)``.

    Returns True if a process was found and the termination signal was
    delivered. If signal delivery fails, ``kill()`` is still attempted as a
    fallback; the return value stays False so a caller can tell that the
    process-group path (which also stops SteamCMD's download children) did
    not work.

    A process that does not exist yet cannot be killed here; use
    :func:`request_cancel`, which records a request the batch checks before
    spawning.
    """
    key = _proc_key(instance_id, mod_id)
    with _live_guard:
        proc = _live_procs.get(key)
        if proc is None:
            return False
        # Mark the whole batch as user-cancelled before killing it, so
        # workshop_download_many reports CANCELLED (not UNKNOWN) for the
        # unfinished ids and keeps finished siblings installable.
        if len(_cancelled_procs) >= _CANCELLED_PROCS_MAX:
            _cancelled_procs.clear()
        _cancelled_procs.add(proc)
    _log.info("终止 SteamCMD 进程（实例 %s, mod %s, pid %s）", instance_id or "", mod_id, proc.pid)
    signaled = False
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
        signaled = True
    except (OSError, subprocess.SubprocessError) as e:
        # Delivery failed (no console / access denied / already gone) — fall
        # through to kill() instead of leaving the process running.
        _log.warning("发送取消信号失败，降级为 kill（mod %s）: %s", mod_id, e)
    try:
        proc.kill()  # belt and braces — CTRL_BREAK is advisory on some setups
    except (OSError, subprocess.SubprocessError):
        with _live_guard:
            _live_procs.pop(key, None)
        return False
    with _live_guard:
        _live_procs.pop(key, None)
    return signaled


def active_download_count() -> int:
    """Number of SteamCMD subprocesses currently running."""
    with _live_guard:
        return len(_live_procs)


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
    _ITEM_SUCCESS_RE = re.compile(r"Success\. Downloaded item (\d+)")
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
        # Items SteamCMD reported as fully downloaded. Read by
        # workshop_download_many when a batch is cancelled mid-flight.
        self.completed: set[str] = set()

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
        success = self._ITEM_SUCCESS_RE.search(line)
        if success and success.group(1) in self._mod_ids:
            # Remember this even if the batch is killed moments later: the
            # content is already on disk and can be installed.
            self.completed.add(success.group(1))
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
