"""Per-mod disk-size snapshots, refreshed in the background.

The real Mods tree is ~150k files / ~20 GB; walking it recursively took 5-8
seconds (measured) and used to happen on every cold ``/api/mods`` and
``/api/dashboard`` request — twice, concurrently, because each endpoint had its
own walker. This module keeps one snapshot per Mods directory:

* readers always return the last snapshot immediately (never block);
* a stale snapshot (TTL 10 min) is refreshed by one daemon thread at a time;
* ``invalidate()`` drops a folder or every snapshot so the next reader starts
  a fresh scan (call it after install/update/delete).

Only directories that look like a mod (``About/About.xml``) are counted, so
``_backups`` and other stray directories inside ``mods_dir`` never inflate the
dashboard. Sizes are stored in bytes; callers convert to MB.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "get_sizes",
    "is_pending",
    "invalidate",
    "size_mb",
    "stop_all",
    "wait_for_scan",
]

_TTL_SECONDS: float = 10 * 60  # 10 minutes


class _Snapshot:
    """State for one Mods directory. Mutated only while holding ``_lock``."""

    __slots__ = ("error", "generation", "scanning", "sizes", "thread", "ts")

    def __init__(self) -> None:
        self.sizes: dict[str, int] = {}
        self.ts: float = 0.0
        self.scanning: bool = False
        self.error: bool = False
        self.thread: threading.Thread | None = None
        # Bumped by invalidate(); a worker whose generation no longer matches
        # must discard its result instead of overwriting a newer scan.
        self.generation: int = 0


_lock = threading.Lock()
_snapshots: dict[str, _Snapshot] = {}


def _key(mods_dir: Path) -> str:
    """Normalize a Mods path so configs differing only in case share a snapshot."""
    return os.path.normcase(os.path.abspath(str(mods_dir)))


def get_sizes(mods_dir: Path, *, exclude: Iterable[str] = ()) -> dict[str, int]:
    """Return ``{folder: bytes}`` from the last snapshot, starting a scan if stale.

    The call never waits for the scan: a cold reader gets ``{}`` (and the
    response may advertise ``size_pending``). ``exclude`` names direct children
    that must not be counted (e.g. ``_backups``); it only applies to a scan
    started by this call.
    """
    path = Path(mods_dir)
    if not path.is_dir():
        return {}

    key = _key(path)
    now = time.monotonic()
    to_start: threading.Thread | None = None

    with _lock:
        snap = _snapshots.get(key)
        if snap is None:
            snap = _Snapshot()
            _snapshots[key] = snap
        # ``ts == 0.0`` is the "never scanned" sentinel written by
        # ``invalidate()``. It must always count as stale: ``time.monotonic()``
        # is seconds since an arbitrary origin (on Windows, roughly boot time),
        # so a fresh snapshot could otherwise look younger than the TTL.
        stale = snap.ts == 0.0 or (now - snap.ts) >= _TTL_SECONDS
        if stale and not snap.scanning:
            snap.scanning = True
            thread = threading.Thread(
                target=_scan_worker,
                args=(key, path, frozenset(exclude), snap.generation),
                name=f"rwmod-size-{path.name}",
                daemon=True,
            )
            snap.thread = thread
            to_start = thread
        result = dict(snap.sizes)

    if to_start is not None:
        try:
            to_start.start()
        except RuntimeError:  # pragma: no cover — interpreter shutting down
            with _lock:
                snap = _snapshots.get(key)
                if snap is not None and snap.thread is to_start:
                    snap.thread = None
                    snap.scanning = False
    return result


def size_mb(mods_dir: Path, folder: str) -> float:
    """Return the last known size of one mod folder in MB (0.0 if unknown)."""
    with _lock:
        snap = _snapshots.get(_key(Path(mods_dir)))
        total = snap.sizes.get(folder, 0) if snap is not None else 0
    return round(total / (1024 * 1024), 2)


def is_pending(mods_dir: Path) -> bool:
    """True while the very first scan is running and no sizes are known yet.

    A stale-but-populated snapshot is *not* pending: its values are served
    while the refresh runs in the background.
    """
    with _lock:
        snap = _snapshots.get(_key(Path(mods_dir)))
        if snap is None:
            return False
        return snap.scanning and not snap.sizes


def invalidate(folder: str | None = None) -> None:
    """Drop one folder's size, or every snapshot, forcing a rescan on next read.

    Any scan that is still in flight is detached: its worker will see a stale
    ``generation`` and discard its result, and the next reader may start a new
    scan immediately instead of waiting for the old thread to finish.
    """
    with _lock:
        for snap in _snapshots.values():
            snap.generation += 1
            if folder is None:
                snap.sizes = {}
            else:
                snap.sizes.pop(folder, None)
            snap.ts = 0.0
            snap.scanning = False
            snap.thread = None


def wait_for_scan(mods_dir: Path, timeout: float = 10.0) -> bool:
    """Join the in-flight scan for ``mods_dir`` (test/CLI helper).

    Returns True when no scan is running for that directory.
    """
    key = _key(Path(mods_dir))
    with _lock:
        snap = _snapshots.get(key)
        thread = snap.thread if snap is not None else None
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
    with _lock:
        snap = _snapshots.get(key)
        return snap is None or not snap.scanning


def stop_all(timeout: float = 10.0) -> None:
    """Join every background scan (daemon threads would not block exit anyway).

    A scan that finished within ``timeout`` is detached from its snapshot so a
    later reader does not see ``scanning=True`` forever. Threads still running
    after the timeout are left alone — they are never killed; ``invalidate()``
    is what detaches those.
    """
    with _lock:
        pending: list[tuple[str, int, threading.Thread]] = []
        for key, snap in _snapshots.items():
            if snap.thread is not None:
                pending.append((key, snap.generation, snap.thread))
    deadline = time.monotonic() + max(0.0, timeout)
    for key, generation, thread in pending:
        remaining = deadline - time.monotonic()
        if thread.is_alive() and remaining > 0:
            thread.join(remaining)
        if thread.is_alive():
            continue
        with _lock:
            current = _snapshots.get(key)
            if (
                current is not None
                and current.generation == generation
                and current.thread is thread
            ):
                current.thread = None
                current.scanning = False


# ── scan internals ────────────────────────────────────────────────


def _scan_worker(
    key: str,
    mods_dir: Path,
    exclude: frozenset[str],
    generation: int,
) -> None:
    try:
        sizes: dict[str, int] | None = _scan_dir(mods_dir, exclude)
    except Exception:  # noqa: BLE001 — a scan must never kill the worker silently
        sizes = None
    with _lock:
        snap = _snapshots.get(key)
        if snap is None or snap.generation != generation:
            # invalidate() ran while we were walking: this result is stale and
            # must not overwrite the snapshot the new scan is filling in.
            return
        snap.scanning = False
        snap.thread = None
        if sizes is None:  # pragma: no cover — unexpected error; retry next read
            snap.error = True
            return
        snap.error = False
        snap.sizes = sizes
        snap.ts = time.monotonic()


def _scan_dir(mods_dir: Path, exclude: frozenset[str]) -> dict[str, int]:
    """Sum file sizes of every mod folder. Uses ``DirEntry.stat`` (no extra syscall)."""
    sizes: dict[str, int] = {}
    try:
        with os.scandir(mods_dir) as entries:
            top_level = list(entries)
    except OSError:
        return sizes

    for entry in top_level:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if entry.name in exclude:
            continue
        # Same predicate the metadata cache uses: a folder without About.xml
        # is not a mod and must not appear in /api/mods or dashboard totals.
        if not os.path.isfile(os.path.join(entry.path, "About", "About.xml")):
            continue

        total = 0
        stack = [entry.path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as children:
                    for child in children:
                        try:
                            if child.is_dir(follow_symlinks=False):
                                stack.append(child.path)
                            elif child.is_file(follow_symlinks=False):
                                total += child.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        sizes[entry.name] = total
    return sizes
