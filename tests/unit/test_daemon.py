"""Unit tests for daemon.py — logging setup and task isolation."""
import logging
import logging.handlers
from pathlib import Path
from unittest.mock import patch

import pytest

import daemon


def test_configure_logging_creates_rotating_handlers(tmp_path):
    """_configure_logging attaches RotatingFileHandlers and creates logs/ dir."""
    daemon._configure_logging(tmp_path)
    root = logging.getLogger()

    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 2, "Expected two RotatingFileHandlers (out.log and error.log)"

    filenames = {Path(h.baseFilename).name for h in rotating}
    assert "out.log" in filenames
    assert "error.log" in filenames


def test_configure_logging_max_bytes(tmp_path):
    """Each RotatingFileHandler respects the LOG_MAX_BYTES threshold."""
    daemon._configure_logging(tmp_path)
    root = logging.getLogger()

    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    for h in rotating:
        assert h.maxBytes == daemon.LOG_MAX_BYTES


def test_configure_logging_creates_logs_dir(tmp_path):
    """logs/ directory is created automatically if missing."""
    logs_dir = tmp_path / "logs"
    assert not logs_dir.exists()
    daemon._configure_logging(tmp_path)
    assert logs_dir.is_dir()


def test_configure_logging_error_log_warning_only(tmp_path):
    """error.log handler only emits WARNING and above."""
    daemon._configure_logging(tmp_path)
    root = logging.getLogger()

    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    err_handlers = [h for h in rotating if Path(h.baseFilename).name == "error.log"]
    assert len(err_handlers) == 1
    assert err_handlers[0].level == logging.WARNING


def test_configure_logging_keeps_stderr_handler(tmp_path):
    """A StreamHandler is kept for launchd stderr capture."""
    daemon._configure_logging(tmp_path)
    root = logging.getLogger()
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                       and not isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(stream_handlers) >= 1
