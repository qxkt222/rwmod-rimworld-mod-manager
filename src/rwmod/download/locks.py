"""Lock registry for concurrent downloads and installs."""

from __future__ import annotations

import os
import threading
from pathlib import Path

# Per-key locks: concurrent downloads of the same mod are serialized so the
# check-then-download sequence is atomic. Prevents TOCTOU races where two
# workers download the same mod and corrupt each other.
_mod_locks: dict[str, threading.Lock] = {}
_mod_locks_guard = threading.Lock()


# Cap on the lock dict so a long-running server (thousands of distinct
# workshop IDs) does not grow it without bound.
_MOD_LOCKS_MAX = 1024


def _lock_for(key: str) -> threading.Lock:
    """Return (creating if needed) the lock registered under *key*."""
    with _mod_locks_guard:
        lock = _mod_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _mod_locks[key] = lock
            # Drop locks nobody is holding or waiting on when the dict grows
            # past the cap. locked() is True while a thread holds OR waits on
            # the lock, so in-use locks are never collected.
            if len(_mod_locks) > _MOD_LOCKS_MAX:
                stale = [k for k, lock2 in _mod_locks.items() if not lock2.locked()]
                for k in stale:
                    _mod_locks.pop(k, None)
                    if len(_mod_locks) <= _MOD_LOCKS_MAX // 2:
                        break
        return lock


def _dest_lock_for(dest: Path) -> threading.Lock:
    """Lock keyed by the *target folder*, not the workshop id.

    Two different workshop IDs can carry the same packageId and therefore
    install into the same folder; a workshop-id lock let both threads delete
    and copy the shared destination concurrently. The destination path is the
    real unit of mutual exclusion during install.
    """
    return _lock_for(f"dest::{os.path.normcase(str(dest))}")
