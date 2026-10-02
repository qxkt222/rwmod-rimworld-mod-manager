"""Tests for logger.py — logging setup must never block startup.

init_logging() runs at import time of rwmod.server, so a failure here means the
server (or a container) cannot start at all. It used to raise when the log
directory was unwritable, which is the normal case for a container whose service
user has no home directory.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from rwmod import logger as logger_mod


@pytest.fixture(autouse=True)
def _restore_root_handlers() -> Iterator[None]:
    """init_logging() mutates the root logger; put it back so adding handlers
    here cannot change what every other test logs."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def test_missing_log_directory_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An unwritable log file degrades to console-only instead of raising."""
    monkeypatch.setattr(logger_mod, "LOG_PATH", tmp_path / "no-such-dir" / ".rwmod.log")
    root = logging.getLogger()
    before = list(root.handlers)

    logger_mod.init_logging()  # must not raise

    # Assert on what this call added — pytest installs handlers of its own on the
    # root logger, so an absolute "no FileHandler anywhere" check is wrong.
    added = [h for h in root.handlers if h not in before]
    assert not any(isinstance(h, logging.FileHandler) for h in added), added
    assert any(isinstance(h, logging.StreamHandler) for h in added), added


def test_writable_path_still_gets_a_file_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The fallback must not replace the normal case."""
    target = tmp_path / ".rwmod.log"
    monkeypatch.setattr(logger_mod, "LOG_PATH", target)

    logger_mod.init_logging()
    logging.getLogger("rwmod.test").warning("hello")

    for handler in logging.getLogger().handlers:
        handler.flush()
    assert target.exists()
    assert "hello" in target.read_text(encoding="utf-8")
