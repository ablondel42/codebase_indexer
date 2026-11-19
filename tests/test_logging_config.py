"""Logging configuration tests verifying stderr-only console output and level checks."""

import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.utils.logging import (
    ROOT_LOGGER_NAME,
    configure_logging,
    get_logger,
    is_logging_configured,
    reset_logging,
)


@pytest.fixture(autouse=True)
def isolate_indexer_logging() -> Iterator[None]:
    """Reset ``indexer`` root logger handlers and state around each test."""
    reset_logging()
    logging.getLogger(ROOT_LOGGER_NAME).propagate = True
    try:
        yield
    finally:
        reset_logging()
        logging.getLogger(ROOT_LOGGER_NAME).propagate = True


class TestGetLogger:
    """Tests for ``get_logger``."""

    def test_get_logger_namespacing(self) -> None:
        """Child loggers attach under the ``indexer`` root namespace."""
        assert ROOT_LOGGER_NAME == "indexer"
        logger = get_logger("storage.services.nodes")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "indexer.storage.services.nodes"
        assert logger is get_logger("storage.services.nodes")
        assert get_logger("").name == "indexer"


class TestConfigureLogging:
    """Tests for ``configure_logging``, ``is_logging_configured``, and ``reset_logging``."""

    def test_console_handler_uses_stderr_not_stdout(self) -> None:
        """Console logging MUST write to sys.stderr so CLI/MCP stdout stays clean."""
        assert is_logging_configured() is False
        configure_logging(level="INFO")
        assert is_logging_configured() is True

        root_logger = logging.getLogger(ROOT_LOGGER_NAME)
        assert root_logger.propagate is False
        stream_handlers = [
            h
            for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].stream is sys.stderr
        assert stream_handlers[0].stream is not sys.stdout

        reset_logging()
        assert is_logging_configured() is False

    def test_configure_logging_skips_if_already_configured(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Second configure_logging call logs a warning to stderr and preserves level."""
        configure_logging(level="INFO")
        configure_logging(level="DEBUG")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "already configured" in captured.err.lower()
        assert logging.getLogger(ROOT_LOGGER_NAME).level == logging.INFO

    def test_configure_logging_respects_valid_levels(self) -> None:
        """Case-insensitive log levels including WARN map to standard logging constants."""
        root_logger = logging.getLogger(ROOT_LOGGER_NAME)
        configure_logging(level="debug")
        assert root_logger.level == logging.DEBUG

        reset_logging()
        configure_logging(level="WARN")
        assert root_logger.level == logging.WARNING

    @pytest.mark.parametrize("bad_level", ["TRACE", "INVALID", "", 123])
    def test_configure_logging_rejects_invalid_levels(self, bad_level: Any) -> None:
        """Unrecognized or non-string log levels raise ValueError."""
        with pytest.raises(ValueError):
            configure_logging(level=bad_level)

    def test_configure_logging_with_file_handler(self, tmp_path: Path) -> None:
        """Optional log_file attaches a UTF-8 FileHandler."""
        log_file = tmp_path / "app.log"
        configure_logging(level="INFO", log_file=log_file)
        root_logger = logging.getLogger(ROOT_LOGGER_NAME)

        assert any(isinstance(h, logging.FileHandler) for h in root_logger.handlers)
        assert log_file.exists()

        get_logger("test_file").info("hello file log")
        for h in root_logger.handlers:
            h.flush()
        assert "hello file log" in log_file.read_text(encoding="utf-8")

    def test_failed_file_handler_preserves_console_level_and_stderr(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If FileHandler fails to open, console handler stays at requested level on stderr."""
        bad_log_path = tmp_path / "missing_dir" / "app.log"
        configure_logging(level="DEBUG", log_file=bad_log_path)

        root_logger = logging.getLogger(ROOT_LOGGER_NAME)
        assert root_logger.level == logging.DEBUG
        stream_handlers = [
            h
            for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].stream is sys.stderr

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "failed to create log file handler" in captured.err.lower()
