"""Offline tracking — records whether Steam/Workshop calls actually succeeded.

State is driven by observed outcomes: the network layer calls ``mark_online`` /
``mark_offline`` around real requests (see ``workshop._request_json``), so what
the UI reads reflects what actually happened.

This replaced a synthetic reachability ping that nothing ever called: the ping
existed, was wired to nothing, and ``/api/status`` therefore reported "online"
forever. Outcome tracking cannot rot that way, and because ``get_status`` does
no I/O it stays cheap to poll and testable without network access.
"""

from __future__ import annotations

import time

__all__ = ["get_status", "mark_offline", "mark_online"]

# ── connectivity state ─────────────────────────────────────────────
# Starts optimistic: nothing has failed yet, and a fresh install should not
# claim to be offline before it has made a single request.
_last_check_time: float = 0
_is_online: bool = True


def get_status() -> dict:
    """Return the last observed connectivity state (no I/O).

    ``last_check`` is a real epoch timestamp (seconds) so the frontend can
    display an absolute "last checked at" time. ``last_check_ago_sec`` is the
    seconds elapsed since that observation.
    """
    if not _last_check_time:
        return {"online": _is_online, "last_check": 0, "last_check_ago_sec": 0}
    return {
        "online": _is_online,
        "last_check": _epoch_last_check(),
        "last_check_ago_sec": round(time.monotonic() - _last_check_time, 1),
    }


def _epoch_last_check() -> float:
    """Return the wall-clock time of the last observation (0 if never)."""
    if not _last_check_time:
        return 0.0
    # _last_check_time is a monotonic timestamp; convert to epoch for display.
    return time.time() - (time.monotonic() - _last_check_time)


def mark_offline() -> None:
    """Record that a Steam request failed to reach the network."""
    global _is_online, _last_check_time
    _is_online = False
    _last_check_time = time.monotonic()


def mark_online() -> None:
    """Record that a Steam request succeeded."""
    global _is_online, _last_check_time
    _is_online = True
    _last_check_time = time.monotonic()
