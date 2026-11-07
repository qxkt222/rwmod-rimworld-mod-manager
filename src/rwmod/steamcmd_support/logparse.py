"""Parsing of SteamCMD's ``logs/workshop_log.txt``.

The log records the real reason for every failure. It is parsed here (pure
functions, no process state) so the downloader can skip pointless retries.
"""

from __future__ import annotations

import re
from pathlib import Path

from rwmod.steamcmd_support.taxonomy import ErrorKind


def classify_reason(reason: str) -> str:
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


def parse_workshop_error(
    log_path: Path,
    app_id: str,
    mod_id: str,
    offset: int = 0,
) -> tuple[str, str]:
    """Parse workshop_log.txt to extract the real error reason.

    Workshop log records look like:
      [AppID 294100] Download item 3565275325 result : Failure
      [AppID 294100] Get details for item 3565275325 failed : File Not Found
      [AppID 294100] Download item 3565275325 result : OK

    Args:
        log_path: Path to workshop_log.txt.
        app_id: The Steam AppID (RimWorld is 294100).
        mod_id: The workshop item ID to look for.
        offset: Bytes offset into the log; only the appended section
                (this process's run) is searched. 0 means the whole file.
    """
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
        rf"\[AppID {re.escape(app_id)}\] Download item {re.escape(mod_id)} result : (.+)"
    )
    result_matches = list(re.finditer(result_pattern, text))

    # Pattern: [AppID 294100] Get details for item {mod_id} failed : {reason}
    detail_pattern = (
        rf"\[AppID {re.escape(app_id)}\] Get details for item {re.escape(mod_id)} failed : (.+)"
    )
    detail_matches = list(re.finditer(detail_pattern, text))

    # No result line for this id in the section *this* run appended: the
    # item was never processed. That is NOT success — defaulting to OK here
    # produced a stream of bogus "SteamCMD 返回成功但未找到下载内容" errors.
    if not result_matches:
        if detail_matches:
            raw_detail = detail_matches[-1].group(1).strip()
            return ErrorKind.UNKNOWN, f"SteamCMD: {raw_detail}"
        return (
            ErrorKind.UNKNOWN,
            "workshop_log.txt 本次运行没有记录该 Mod 的下载结果（SteamCMD 可能未启动或提前退出）",
        )

    raw_reason = result_matches[-1].group(1).strip()
    error_kind = classify_reason(raw_reason)
    error_detail = f"SteamCMD: {raw_reason}"

    # Detail lines give more specific info (e.g. "Wrong AppID 241100")
    if detail_matches:
        raw_detail = detail_matches[-1].group(1).strip()
        if raw_detail and raw_detail != error_kind.replace("_", " ").title():
            error_detail = f"SteamCMD: {raw_detail}"

    return error_kind, error_detail
