"""Structured logging — writes to ~/.rwmod.log with rotation."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["init_logging", "get_log", "LOG_PATH"]

LOG_PATH = Path.home() / ".rwmod.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def init_logging(level: int = logging.INFO) -> None:
    """Configure root logging: rotating file handler + console handler.

    The file handler is best-effort. This runs at import time of rwmod.server, and
    an unwritable or absent home directory — a container whose service user has no
    home, a CI sandbox, a systemd unit with a read-only $HOME — must not stop the
    process from starting. Logging then degrades to console-only, which is what a
    container wants anyway (docker logs).
    """
    root = logging.getLogger()
    root.setLevel(level)

    # File handler with rotation
    try:
        fh = RotatingFileHandler(
            str(LOG_PATH), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as e:
        # No handlers are configured yet, so this reaches stderr via lastResort.
        logging.getLogger(__name__).warning("无法写入日志文件 %s，仅输出到控制台: %s", LOG_PATH, e)
    else:
        fh.setFormatter(_fmt)
        fh.setLevel(level)
        root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(_fmt)
    ch.setLevel(level)
    root.addHandler(ch)

    # Silence noisy libs
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


def get_log(name: str) -> logging.Logger:
    return logging.getLogger(name)
