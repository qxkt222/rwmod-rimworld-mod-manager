"""Structured download results shared by the single and batch pipelines."""

from __future__ import annotations

from dataclasses import dataclass

from rwmod.steamcmd import ErrorKind

# Kinds produced by the downloader itself (not by SteamCMD's workshop log).
# They live here rather than in steamcmd.ErrorKind because they describe our
# pipeline, not a SteamCMD result line.
KIND_INSTALL_FAILED = "install_failed"
KIND_COLLECTION_PROBE_FAILED = "collection_probe_failed"
KIND_COLLECTION_EMPTY = "collection_empty"

_OUTCOME_HUMAN: dict[str, str] = {
    KIND_INSTALL_FAILED: "下载完成但安装失败",
    KIND_COLLECTION_PROBE_FAILED: "无法确认是否为合集（Steam API 不可用）",
    KIND_COLLECTION_EMPTY: "合集为空或无法获取子项",
}


@dataclass(frozen=True)
class DownloadOutcome:
    """Structured result of one download, safe to surface in the queue/UI.

    ``error_detail`` is for hover/tooltips and logs — keep it out of compact
    UI rows. ``error_kind`` is a stable machine code (``ErrorKind.*`` or one
    of the ``KIND_*`` constants above).
    """

    ok: bool
    error_kind: str = ""
    error_detail: str = ""


class BatchResult(dict[str, bool]):
    """``{mod_id: ok}`` plus the per-id structured outcomes.

    A dict subclass so every existing caller that expects
    ``dict[str, bool]`` keeps working; ``.outcomes`` carries the reasons.
    Queue/router callers read ``.outcomes`` instead of taking a new keyword,
    which also keeps older test doubles (plain dict returns) valid.
    """

    outcomes: dict[str, DownloadOutcome]

    def __init__(
        self,
        ok: dict[str, bool],
        outcomes: dict[str, DownloadOutcome],
    ) -> None:
        super().__init__(ok)
        self.outcomes = outcomes


def explain_outcome(kind: str) -> str:
    """Human-readable, short reason for a download outcome kind."""
    return _OUTCOME_HUMAN.get(kind) or ErrorKind.explain(kind)
