"""SteamCMD result taxonomy — error kinds and the structured result object.

Kept separate from process orchestration so the log/verdict logic can be
tested and reasoned about without launching anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ErrorKind:
    """SteamCMD error categories from workshop_log.txt."""

    OK = "ok"
    FAILURE = "failure"  # depot/manifest error — mod exists but can't download
    FILE_NOT_FOUND = "file_not_found"  # mod removed from workshop
    ACCESS_DENIED = "access_denied"  # private/hidden mod
    NO_MATCH = "no_match"  # wrong game AppID
    TIMEOUT = "timeout"
    # SteamCMD itself cannot run here (e.g. its directory contains non-ASCII
    # characters). Nothing about the mod is wrong — retrying is pointless.
    ENVIRONMENT = "environment"
    # The user cancelled an in-flight batch. The process was killed, so the
    # unfinished items must be re-queued (or failed after the retry budget),
    # not retried immediately. Deliberately NOT in NON_RETRYABLE: callers
    # handle it explicitly instead of logging it as a permanent failure.
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    # Errors that will NEVER succeed on retry → skip retry + Skymods
    NON_RETRYABLE = {FAILURE, FILE_NOT_FOUND, ACCESS_DENIED, NO_MATCH, ENVIRONMENT}

    _HUMAN: dict[str, str] = {
        OK: "下载成功",
        FAILURE: "Mod 存在但文件不可用（可能已被作者删除或设为私密）",
        FILE_NOT_FOUND: "Mod 不存在（已从创意工坊移除）",
        ACCESS_DENIED: "Mod 为私密/隐藏状态，无法匿名下载",
        NO_MATCH: "不是 RimWorld 的 Mod（属于其他游戏）",
        TIMEOUT: "SteamCMD 超时无响应",
        ENVIRONMENT: "SteamCMD 运行环境不可用（路径含非 ASCII 字符等）",
        CANCELLED: "下载已取消（同批其他项被取消）",
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
    # True when SteamCMD had already printed "Success. Downloaded item <id>"
    # before the batch was killed. Lets a cancelled batch install the items
    # that actually finished and re-queue only the unfinished ones.
    completed: bool = False


def _decide_download_result(
    mod_id: str,
    error_kind: str,
    error_detail: str,
    content_exists: bool,
    lines: list[str],
) -> DownloadResult:
    """Combine the workshop_log verdict with the on-disk state into a result.

    The filesystem is trusted *only* when the log has no verdict for this id
    (UNKNOWN). An explicit failure must never be masked by content left over
    from an earlier download — that is exactly how a failed update used to be
    reported as success and have its ``last_updated`` refreshed.
    """
    if error_kind == ErrorKind.OK:
        if content_exists:
            return DownloadResult(
                success=True,
                mod_id=mod_id,
                error_kind=ErrorKind.OK,
                output_lines=lines,
            )
        # SteamCMD said OK but the files are not on disk → failure.
        return DownloadResult(
            success=False,
            mod_id=mod_id,
            error_kind=ErrorKind.FAILURE,
            error_detail="SteamCMD 报告下载成功，但磁盘上找不到下载内容",
            output_lines=lines,
        )
    if error_kind == ErrorKind.UNKNOWN and content_exists:
        # No usable log line, but the files are on disk (e.g. the log was
        # rotated mid-run). Trust the filesystem over the log.
        return DownloadResult(
            success=True,
            mod_id=mod_id,
            error_kind=ErrorKind.OK,
            output_lines=lines,
        )
    # Explicit failure — keep its classification and detail untouched.
    return DownloadResult(
        success=False,
        mod_id=mod_id,
        error_kind=error_kind,
        error_detail=error_detail,
        output_lines=lines,
    )
