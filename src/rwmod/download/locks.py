"""Lock registry for concurrent downloads and installs."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from weakref import WeakValueDictionary

# Per-key locks: concurrent downloads of the same mod are serialized so the
# check-then-download sequence is atomic. Prevents TOCTOU races where two
# workers download the same mod and corrupt each other.
#
# The registry holds *weak* references: a lock stays registered only while at
# least one caller still references it. Once the last worker drops its
# reference the entry disappears automatically, so a long-running server
# handling thousands of distinct workshop ids cannot grow the dict without
# bound. There is deliberately no eviction pass: an eviction pass raced with
# callers that had already received a lock but had not acquired it yet and
# could pop that live lock, handing the same key to two threads at once.
_mod_locks: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
_mod_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    """Return (creating if needed) the lock registered under *key*."""
    with _mod_locks_guard:
        lock = _mod_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _mod_locks[key] = lock
        return lock


def _dest_lock_for(dest: Path) -> threading.Lock:
    """Lock keyed by the *target folder*, not the workshop id.

    Two different workshop IDs can carry the same packageId and therefore
    install into the same folder; a workshop-id lock let both threads delete
    and copy the shared destination concurrently. The destination path is the
    real unit of mutual exclusion during install.
    """
    return _lock_for(f"dest::{os.path.normcase(str(dest))}")
